"""WS-коллектор снимков стакана Polymarket.

Подписывается на рыночный WS CLOB, пишет book_snapshots / tick_changes /
gap_intervals / recon_checks в duckdb (data/pm.duckdb) и экспортирует
parquet-проекцию для сверки.

Известные факты протокола (см. SCHEMAS.md, раздел WS):
- подписка: {"type": "market", "assets_ids": [token_id, ...]};
- типы сообщений: book (полный снимок), price_change (дельты, массив
  price_changes с лучшими ценами прямо в записи), last_trade_price,
  book_array (стартовый список);
- серверной нумерации НЕТ -> детектор потерь = recon_checks;
- seq — локальный счётчик приёмника, продолжается от max(seq) в базе.

После обрыва: пишется gap_intervals(reason=disconnect) ПО ВСЕМ токенам,
REST-бэкфилл /book (строки source='rest_backfill', наблюдением внутри
разрыва НЕ считаются), затем продолжение. Смена рынка — НЕ разрыв:
периодический re-discovery и переподписка, в gap_intervals не пишется.

Запуск:  python -m src.collect.ws_collector --minutes 15 --vertical crypto
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import httpx
import websockets

from . import schema, store
from .recon import LiveBook, recon_check
from .schema import SIDE_BUY, SIDE_SELL, SOURCE_REST_BACKFILL, SOURCE_WS
from src.validate.book_poller import parse_book, vwap_first_qty

log = logging.getLogger("collect.ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BOOK_URL = "https://clob.polymarket.com/book"
GAMMA_URL = "https://gamma-api.polymarket.com"
USER_AGENT = "pm-collect/0.1 (personal research)"

PING_INTERVAL_S = 20.0
# Поллимаркет-сервер подтверждает ping->pong быстро (0.1s, зонд), но при
# бурстах на одном цикле событий ответ задерживается и сервер закрывает
# 1011 'keepalive ping timeout'. Клиентский таймаут делаем намного больше
# интервала, чтобы НЕ отрубать соединение самому по задержке pong'а: рваный
# обрыв ловится серверным снэпшотом/recon, а не клиентским пингом.
PING_TIMEOUT_S = 90.0
RECV_TIMEOUT_S = 1.0
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 60.0
# Мягкий флаг тишины: молчащий рынок НЕ обязан быть разрывом (порог не
# проверен на живых данных). Рынки up/down живут 5-15 минут и умирают тихо.
SILENCE_THRESHOLD_S = 120.0
MARKET_RECHECK_S = 60.0
EXPORT_INTERVAL_S = 60.0
MAX_DEDUP = 20000
VWAP_QUANTITY = 100.0

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def parse_ts(value: Any) -> int | None:
    """Серверная метка: 13-значное целое epoch ms. Нет/не парсится -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_levels(raw: Any) -> tuple[tuple[float, float], ...]:
    """Список {price, size} -> пары float. Мусор пропускается."""
    out: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return ()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        price = _as_float(entry.get("price"))
        size = _as_float(entry.get("size"))
        if price is None or size is None:
            continue
        out.append((price, size))
    return tuple(out)


@dataclass(frozen=True)
class BookEvent:
    """Полный серверный снимок книги одного token_id."""

    token_id: str
    market_id: str | None
    ts_server_ms: int | None
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    raw: str


@dataclass(frozen=True)
class DeltaEvent:
    """Одна дельта price_change (изменение одного уровня)."""

    token_id: str
    market_id: str | None
    ts_server_ms: int | None
    side: str
    price: float
    size: float
    best_bid: float | None
    best_ask: float | None
    raw: str


@dataclass(frozen=True)
class TradeEvent:
    """last_trade_price (книгу не меняет)."""

    token_id: str
    market_id: str | None
    ts_server_ms: int | None
    side: str | None
    price: float
    size: float
    raw: str


Event = BookEvent | DeltaEvent | TradeEvent


def interpret_message(payload: Any, ts_recv_ms: int) -> tuple[list[Event], dict[str, str]]:
    """Сырое WS-сообщение -> события и {token_id: market_id}.

    book_array (список) обрабатывается как несколько сообщений. Неизвестный
    тип сообщения возвращает пустой список событий (счётчик в статистике).
    """
    events: list[Event] = []
    markets: dict[str, str] = {}
    if isinstance(payload, list):
        for item in payload:
            ev, mk = interpret_message(item, ts_recv_ms)
            events.extend(ev)
            markets.update(mk)
        return events, markets
    if not isinstance(payload, dict):
        return events, markets

    event_type = payload.get("event_type")
    ts_server = parse_ts(payload.get("timestamp"))
    market_id = payload.get("market")
    market_id = str(market_id) if market_id else None
    raw = json.dumps(payload, ensure_ascii=False)

    if event_type == "book":
        token = payload.get("asset_id")
        if token:
            markets[token] = market_id if market_id else ""
            events.append(
                BookEvent(
                    token_id=token,
                    market_id=market_id,
                    ts_server_ms=ts_server,
                    bids=_parse_levels(payload.get("bids")),
                    asks=_parse_levels(payload.get("asks")),
                    raw=raw,
                )
            )
    elif event_type == "price_change":
        entries = payload.get("price_changes")
        if isinstance(entries, list):
            for e in entries:
                if not isinstance(e, dict):
                    continue
                token = e.get("asset_id")
                price = _as_float(e.get("price"))
                size = _as_float(e.get("size"))
                if not token or price is None or size is None:
                    continue
                markets[token] = market_id if market_id else ""
                events.append(
                    DeltaEvent(
                        token_id=token,
                        market_id=market_id,
                        ts_server_ms=ts_server,
                        side=e.get("side"),
                        price=price,
                        size=size,
                        best_bid=_as_float(e.get("best_bid")),
                        best_ask=_as_float(e.get("best_ask")),
                        raw=raw,
                    )
                )
    elif event_type == "last_trade_price":
        token = payload.get("asset_id")
        price = _as_float(payload.get("price"))
        size = _as_float(payload.get("size"))
        if token and price is not None and size is not None:
            markets[token] = market_id if market_id else ""
            events.append(
                TradeEvent(
                    token_id=token,
                    market_id=market_id,
                    ts_server_ms=ts_server,
                    side=payload.get("side"),
                    price=price,
                    size=size,
                    raw=raw,
                )
            )
    return events, markets


def _best_level(
    levels: Sequence[tuple[float, float]], *, prefer_highest: bool
) -> tuple[float, float] | None:
    if not levels:
        return None
    best = levels[0]
    for price, size in levels[1:]:
        if (prefer_highest and price > best[0]) or (
            not prefer_highest and price < best[0]
        ):
            best = (price, size)
    return best


def snapshot_from_levels(
    *,
    token_id: str,
    ts_server_ms: int | None,
    levels_bids: Sequence[tuple[float, float]],
    levels_asks: Sequence[tuple[float, float]],
    ts_recv_ms: int,
    seq: int,
    source: str,
) -> dict[str, Any]:
    """Строка book_snapshots из полного снимка (WS book или REST /book)."""
    bid = _best_level(levels_bids, prefer_highest=True)
    ask = _best_level(levels_asks, prefer_highest=False)
    best_bid = bid[0] if bid else None
    best_ask = ask[0] if ask else None
    return {
        "ts_recv_ms": ts_recv_ms,
        "ts_server_ms": ts_server_ms,
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid[1] if bid else None,
        "ask_size": ask[1] if ask else None,
        "spread": (
            round(best_ask - best_bid, 8)
            if best_bid is not None and best_ask is not None
            else None
        ),
        "vwap_bid_100": (
            vwap_first_qty(levels_bids, best_first=True) if levels_bids else None
        ),
        "vwap_ask_100": (
            vwap_first_qty(levels_asks, best_first=False) if levels_asks else None
        ),
        "book_age_ms": (ts_recv_ms - ts_server_ms) if ts_server_ms is not None else None,
        "seq": seq,
        "source": source,
    }


def snapshot_from_delta(
    *,
    token_id: str,
    ts_server_ms: int | None,
    best_bid: float | None,
    best_ask: float | None,
    livebook: LiveBook,
    ts_recv_ms: int,
    seq: int,
) -> dict[str, Any]:
    """Строка book_snapshots из price_change: лучшие цены из сообщения,
    размеры и vwap — из состояния LiveBook после применения дельты."""
    if best_bid is None:
        bb = livebook.best_bid()
        best_bid = bb[0] if bb else None
    if best_ask is None:
        ba = livebook.best_ask()
        best_ask = ba[0] if ba else None
    bid_size = livebook.bids.get(best_bid) if best_bid is not None else None
    ask_size = livebook.asks.get(best_ask) if best_ask is not None else None
    return {
        "ts_recv_ms": ts_recv_ms,
        "ts_server_ms": ts_server_ms,
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": (
            round(best_ask - best_bid, 8)
            if best_bid is not None and best_ask is not None
            else None
        ),
        "vwap_bid_100": livebook.vwap("bid", VWAP_QUANTITY),
        "vwap_ask_100": livebook.vwap("ask", VWAP_QUANTITY),
        "book_age_ms": (ts_recv_ms - ts_server_ms) if ts_server_ms is not None else None,
        "seq": seq,
        "source": SOURCE_WS,
    }


def tick_row(
    *,
    ts_recv_ms: int,
    ts_server_ms: int | None,
    token_id: str,
    event_type: str,
    side: str | None,
    price: float | None,
    size: float | None,
    best_bid: float | None,
    best_ask: float | None,
    raw: str,
    seq: int,
) -> dict[str, Any]:
    return {
        "ts_recv_ms": ts_recv_ms,
        "ts_server_ms": ts_server_ms,
        "token_id": token_id,
        "event_type": event_type,
        "side": side,
        "price": price,
        "size": size,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "raw": raw,
        "seq": seq,
    }


def seq_gap_row(
    *,
    token_id: str,
    seq_prev: int,
    seq_new: int,
    ts_from_ms: int,
    ts_to_ms: int,
    reason: str = "time_gap",
) -> dict[str, Any]:
    """Строка gap_intervals для разрыва нумерации seq.

    Локальный счётчик не даёт разрывов в штатном режиме; функция нужна для
    защиты и для теста (n_missing = seq_new - seq_prev - 1).
    """
    if seq_new <= seq_prev:
        raise ValueError(
            f"seq_new={seq_new} не больше seq_prev={seq_prev}: это не разрыв"
        )
    return {
        "token_id": token_id,
        "start_ms": ts_from_ms,
        "end_ms": ts_to_ms,
        "reason": reason,
        "n_missing": seq_new - seq_prev - 1,
    }


def git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def utc_ms() -> int:
    return int(time.time() * 1000)


class Collector:
    """Приёмник WS-сообщений: LiveBook, строки в duckdb, гэпы, статистика.

    Сетевой части не содержит (тестируется без сети): события подаются через
    handle_event, разрывы — через record_disconnect.
    """

    def __init__(self, con: Any, *, now_ms: int) -> None:
        self.con = con
        self.livebooks: dict[str, LiveBook] = {}
        self._seq_snaps: dict[str, int] = {}
        self._seq_ticks: dict[str, int] = {}
        self._dedup: deque[str] = deque()
        self._dedup_set: set[str] = set()
        self.pending_resync: set[str] = set()
        self._resync_from: dict[str, int] = {}
        self._silence_open = False
        self.subscribed_tokens: list[str] = []
        self.last_activity_ms = now_ms
        self.stats = {
            "messages": 0,
            "unknown_types": 0,
            "events": 0,
            "events_skipped_dedup": 0,
            "snapshots": 0,
            "ticks": 0,
            "recons": 0,
            "recons_mismatch": 0,
            "reconnects": 0,
        }
        self.heartbeat = {
            "ping_interval_s": PING_INTERVAL_S,
            "max_silence_s": 0.0,
            "n_silence_episodes": 0,
            "n_pings_fired": 0,
        }

    def _seq_snap(self, token_id: str) -> int:
        if token_id not in self._seq_snaps:
            self._seq_snaps[token_id] = store.current_seq(self.con, "book_snapshots", token_id)
        value = self._seq_snaps[token_id]
        self._seq_snaps[token_id] += 1
        return value

    def _seq_tick(self, token_id: str) -> int:
        if token_id not in self._seq_ticks:
            self._seq_ticks[token_id] = store.current_seq(self.con, "tick_changes", token_id)
        value = self._seq_ticks[token_id]
        self._seq_ticks[token_id] += 1
        return value

    def _is_duplicate(self, key: str) -> bool:
        """Анти-дубль при двойной подписке (токен и его комплемент)."""
        if key in self._dedup_set:
            self.stats["events_skipped_dedup"] += 1
            return True
        self._dedup_set.add(key)
        self._dedup.append(key)
        if len(self._dedup) > MAX_DEDUP:
            old = self._dedup.popleft()
            self._dedup_set.discard(old)
        return False

    def _dedup_key(self, event: Event) -> str:
        if isinstance(event, BookEvent):
            return f"book|{event.token_id}|{event.ts_server_ms}|{event.raw}"
        if isinstance(event, DeltaEvent):
            return (
                f"delta|{event.token_id}|{event.ts_server_ms}|{event.price}|"
                f"{event.size}|{event.side}|{event.best_bid}|{event.best_ask}"
            )
        return (
            f"trade|{event.token_id}|{event.ts_server_ms}|{event.price}|"
            f"{event.size}|{event.side}"
        )

    def _livebook(self, token_id: str) -> LiveBook:
        book = self.livebooks.get(token_id)
        if book is None:
            book = LiveBook()
            self.livebooks[token_id] = book
        return book

    def handle_event(self, event: Event, ts_recv_ms: int) -> None:
        """Применить событие: обновить LiveBook и записать строки."""
        self.stats["events"] += 1
        self.last_activity_ms = ts_recv_ms

        if isinstance(event, BookEvent):
            self._handle_book(event, ts_recv_ms)
        elif isinstance(event, DeltaEvent):
            self._handle_delta(event, ts_recv_ms)
        elif isinstance(event, TradeEvent):
            self._handle_trade(event, ts_recv_ms)

    def _handle_book(self, event: BookEvent, ts_recv_ms: int) -> None:
        live = self._livebook(event.token_id)
        seq = self._seq_snap(event.token_id)
        store.insert_row(
            self.con,
            "book_snapshots",
            snapshot_from_levels(
                token_id=event.token_id,
                ts_server_ms=event.ts_server_ms,
                levels_bids=event.bids,
                levels_asks=event.asks,
                ts_recv_ms=ts_recv_ms,
                seq=seq,
                source=SOURCE_WS,
            ),
        )
        self.stats["snapshots"] += 1

        theirs_bids = {p: s for p, s in event.bids}
        theirs_asks = {p: s for p, s in event.asks}
        rc = recon_check(
            ts_recv_ms=ts_recv_ms,
            token_id=event.token_id,
            ours=live,
            theirs_bids=theirs_bids,
            theirs_asks=theirs_asks,
        )
        store.insert_row(self.con, "recon_checks", rc)
        self.stats["recons"] += 1
        if rc["verdict"] == "mismatch":
            self.stats["recons_mismatch"] += 1

        if event.token_id in self.pending_resync:
            store.insert_row(
                self.con,
                "gap_intervals",
                {
                    "token_id": event.token_id,
                    "start_ms": self._resync_from.get(event.token_id, ts_recv_ms),
                    "end_ms": ts_recv_ms,
                    "reason": "server_resync",
                    "n_missing": None,
                },
            )
            self.pending_resync.discard(event.token_id)

        live.set_book(event.bids, event.asks)

        store.insert_row(
            self.con,
            "tick_changes",
            tick_row(
                ts_recv_ms=ts_recv_ms,
                ts_server_ms=event.ts_server_ms,
                token_id=event.token_id,
                event_type="book",
                side=None,
                price=None,
                size=None,
                best_bid=None,
                best_ask=None,
                raw=event.raw,
                seq=self._seq_tick(event.token_id),
            ),
        )
        self.stats["ticks"] += 1

    def _handle_delta(self, event: DeltaEvent, ts_recv_ms: int) -> None:
        live = self._livebook(event.token_id)
        if event.side not in (SIDE_BUY, SIDE_SELL):
            self.stats["unknown_types"] += 1
            return
        if self._is_duplicate(self._dedup_key(event)):
            return
        live.apply_change(event.side, event.price, event.size)

        seq = self._seq_snap(event.token_id)
        store.insert_row(
            self.con,
            "book_snapshots",
            snapshot_from_delta(
                token_id=event.token_id,
                ts_server_ms=event.ts_server_ms,
                best_bid=event.best_bid,
                best_ask=event.best_ask,
                livebook=live,
                ts_recv_ms=ts_recv_ms,
                seq=seq,
            ),
        )
        self.stats["snapshots"] += 1

        store.insert_row(
            self.con,
            "tick_changes",
            tick_row(
                ts_recv_ms=ts_recv_ms,
                ts_server_ms=event.ts_server_ms,
                token_id=event.token_id,
                event_type="price_change",
                side=event.side,
                price=event.price,
                size=event.size,
                best_bid=event.best_bid,
                best_ask=event.best_ask,
                raw=event.raw,
                seq=self._seq_tick(event.token_id),
            ),
        )
        self.stats["ticks"] += 1

    def _handle_trade(self, event: TradeEvent, ts_recv_ms: int) -> None:
        if self._is_duplicate(self._dedup_key(event)):
            return
        store.insert_row(
            self.con,
            "tick_changes",
            tick_row(
                ts_recv_ms=ts_recv_ms,
                ts_server_ms=event.ts_server_ms,
                token_id=event.token_id,
                event_type="last_trade_price",
                side=event.side,
                price=event.price,
                size=event.size,
                best_bid=None,
                best_ask=None,
                raw=event.raw,
                seq=self._seq_tick(event.token_id),
            ),
        )
        self.stats["ticks"] += 1

    def observe_silence(self, now_ms: int) -> None:
        """Мягкий флаг тишины: молчание дольше порога — time_gap, не разрыв."""
        idle_s = (now_ms - self.last_activity_ms) / 1000.0
        self.heartbeat["max_silence_s"] = max(self.heartbeat["max_silence_s"], idle_s)
        if idle_s >= PING_INTERVAL_S:
            self.heartbeat["n_pings_fired"] += 1
        if idle_s >= SILENCE_THRESHOLD_S:
            if not self._silence_open:
                self._silence_open = True
                self.heartbeat["n_silence_episodes"] += 1
                for token_id in self.subscribed_tokens:
                    store.insert_row(
                        self.con,
                        "gap_intervals",
                        {
                            "token_id": token_id,
                            "start_ms": self.last_activity_ms,
                            "end_ms": now_ms,
                            "reason": "time_gap",
                            "n_missing": None,
                        },
                    )
        else:
            self._silence_open = False

    def record_disconnect(
        self,
        tokens: Sequence[str],
        *,
        ts_from_ms: int,
        ts_to_ms: int,
    ) -> None:
        """Обрыв соединения: gap_intervals(reason=disconnect) по всем токенам."""
        self.stats["reconnects"] += 1
        for token_id in tokens:
            store.insert_row(
                self.con,
                "gap_intervals",
                {
                    "token_id": token_id,
                    "start_ms": ts_from_ms,
                    "end_ms": ts_to_ms,
                    "reason": "disconnect",
                    "n_missing": None,
                },
            )
        self.pending_resync.update(tokens)
        self._resync_from = {t: ts_to_ms for t in tokens}


class WSHandler:
    """Сетевой слой: разбор сообщений и запись в collector."""

    def __init__(
        self,
        collector: Collector,
        *,
        tokens: list[str],
        market_slugs: dict[str, str],
    ) -> None:
        self.collector = collector
        self.tokens = list(tokens)
        self.market_slugs = dict(market_slugs)
        self._market_id_written: set[str] = set()

    def handle_message(self, raw: str, ts_recv_ms: int) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        self.collector.stats["messages"] += 1
        try:
            payload = json.loads(raw)
        except ValueError:
            self.collector.stats["unknown_types"] += 1
            return
        events, markets = interpret_message(payload, ts_recv_ms)
        for token_id, market_id in markets.items():
            if market_id and token_id not in self._market_id_written:
                store.update_market_field(self.collector.con, token_id, market_id=market_id)
                self._market_id_written.add(token_id)
        for event in events:
            self.collector.handle_event(event, ts_recv_ms)
        if not events:
            self.collector.stats["unknown_types"] += 1


def _discover_crypto(client: httpx.Client) -> tuple[list[str], dict[str, str]]:
    """Живые crypto up/down токены через src.validate.discovery."""
    from src.validate.discovery import updown_outcomes

    res = updown_outcomes(client)
    tokens: list[str] = []
    slugs: dict[str, str] = {}
    for o in res.outcomes:
        tokens.append(o.token_id)
        slugs[o.token_id] = o.market_slug
    return tokens, slugs


BACKFILL_CONCURRENCY = 16


async def _rest_backfill(
    collector: Collector,
    tokens: Sequence[str],
    *,
    ts_recv_ms: int,
) -> None:
    """REST /book после обрыва: строки source='rest_backfill' + реинициализация.

    Параллельно: последовательный обход 84 токенов занимал 7-11 c и почти
    целиком съедал окно server_resync (12-34 c на каждый обрыв, что ломало
    гейт <5% покрытия). Пул BACKFILL_CONCURRENCY без блокировок цикла событий:
    во время бэкфилла не приостанавливается обработка пингов сервера.

    Наблюдением внутри разрыва НЕ считаются: анализ читает только source='ws'.
    """

    async def _one(token_id: str) -> None:
        try:
            r = await _async_client.get(BOOK_URL, params={"token_id": token_id})
        except httpx.HTTPError as exc:
            log.warning("backfill %s: %r", token_id[:12], exc)
            return
        if r.status_code in _RETRY_STATUS:
            return
        if r.status_code != 200:
            # мёртвый/тонкий рынок (404 No orderbook exists) — не разрыв,
            # пропускаем, но сбрасываем признак инициализации: первый
            # WS-снимок после обрыва должен стать warmup, а не ложным
            # mismatch против устаревшей докэбрывной книги.
            live = collector._livebook(token_id)
            live.initialized = False
            return
        try:
            pb = parse_book(r.json())
        except ValueError:
            return
        seq = collector._seq_snap(token_id)
        store.insert_row(
            collector.con,
            "book_snapshots",
            snapshot_from_levels(
                token_id=token_id,
                ts_server_ms=pb.server_ts_ms,
                levels_bids=pb.levels_bids,
                levels_asks=pb.levels_asks,
                ts_recv_ms=ts_recv_ms,
                seq=seq,
                source=SOURCE_REST_BACKFILL,
            ),
        )
        live = collector._livebook(token_id)
        live.set_book(pb.levels_bids, pb.levels_asks)
        # /book кэширован и устарел на десятки событий (проверено в
        # задачах 3.6). Бэкфилл НЕ базис для recon: первый WS-снимок
        # после обрыва сравнивать с ним нельзя (ложный mismatch).
        live.initialized = False

    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": USER_AGENT}
    ) as _async_client:
        sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)

        async def _guarded(token_id: str) -> None:
            async with sem:
                await _one(token_id)

        await asyncio.gather(*(_guarded(t) for t in tokens))


def _register_markets(
    collector: Collector,
    tokens: list[str],
    slugs: dict[str, str],
    *,
    start_ms: int,
) -> None:
    for token_id in tokens:
        store.upsert_market(
            collector.con,
            {
                "token_id": token_id,
                "market_id": None,
                "event_id": slugs.get(token_id),
                "vertical": "crypto",
                "start_ms": start_ms,
                "end_ms": None,
                "resolved": False,
            },
        )


def _resubscribe(handler: WSHandler, collector: Collector) -> bool:
    """Периодический re-discovery: новые токены -> markets_tracked + подписка."""
    try:
        with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as gc:
            tokens, slugs = _discover_crypto(gc)
    except Exception as exc:  # noqa: BLE001
        log.warning("re-discovery не удался: %r", exc)
        return False
    known = set(handler.tokens)
    new = [t for t in tokens if t not in known]
    if not new:
        return False
    _register_markets(collector, new, slugs, start_ms=utc_ms())
    handler.tokens = list(tokens)
    collector.subscribed_tokens = list(tokens)
    log.info("re-discovery: новых токенов %d (всего %d)", len(new), len(handler.tokens))
    return True


async def _run_connection(
    handler: WSHandler,
    collector: Collector,
    *,
    deadline: int,
    export_root: Path,
) -> None:
    """Одна стабильная WS-сессия: подключение, приём, ротация, экспорт.
    Переподключение с экспоненциальной задержкой внутри. Выход по deadline."""
    delay = RECONNECT_BASE_S
    last_recheck = time.monotonic()
    last_export = time.monotonic()
    while True:
        if utc_ms() >= deadline:
            return
        try:
            ws = await websockets.connect(
                WS_URL,
                ping_interval=PING_INTERVAL_S,
                ping_timeout=PING_TIMEOUT_S,
                open_timeout=30.0,
                user_agent_header=USER_AGENT,
                max_size=2**22,
            )
        except (OSError, websockets.WebSocketException) as exc:
            log.warning("подключение не удалось: %r; повтор через %.1f c", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)
            continue
        delay = RECONNECT_BASE_S
        try:
            async with ws:
                log.info("подключено; подписка %d токенов", len(handler.tokens))
                await ws.send(json.dumps({"type": "market", "assets_ids": handler.tokens}))
                await _rest_backfill(collector, handler.tokens, ts_recv_ms=utc_ms())
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        collector.observe_silence(utc_ms())
                        if utc_ms() >= deadline:
                            return
                        continue
                    handler.handle_message(raw, utc_ms())
                    # Бар: обработка сообщения синхронна (вставки в duckdb).
                    # Явный yield даёт циклу событий обслужить управляющие кадры
                    # (pong на пинг сервера), иначе при бурстах сервер закрывает
                    # соединение 1011 'keepalive ping timeout'.
                    await asyncio.sleep(0)

                    now = time.monotonic()
                    if now - last_recheck >= MARKET_RECHECK_S:
                        last_recheck = now
                        if _resubscribe(handler, collector):
                            await ws.send(
                                json.dumps({"type": "market", "assets_ids": handler.tokens})
                            )
                    if now - last_export >= EXPORT_INTERVAL_S:
                        last_export = now
                        store.export_tables(collector.con, export_root)
        except websockets.ConnectionClosed as exc:
            log.warning("соединение закрыто: %r", exc)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log.warning("ошибка сокета: %r", exc)
        if utc_ms() >= deadline:
            return
        collector.record_disconnect(
            collector.subscribed_tokens,
            ts_from_ms=collector.last_activity_ms,
            ts_to_ms=utc_ms(),
        )


async def run(
    *,
    minutes: float,
    db_path: Path,
    export_root: Path,
) -> int:
    """Основной цикл: сессия, discovery, приём, гэпы, экспорт."""
    con = store.connect(db_path)
    session_id = uuid.uuid4().hex[:12]
    started_ms = utc_ms()
    deadline = started_ms + int(minutes * 60_000)

    # process_restart: были прошлые сессии -> документируем простой по токенам
    prev_start, prev_end = store.last_session_bounds(con)
    if prev_start is not None:
        from_ts = prev_end if prev_end is not None else prev_start
        for token_id in store.tracked_tokens(con):
            store.insert_row(
                con,
                "gap_intervals",
                {
                    "token_id": token_id,
                    "start_ms": from_ts,
                    "end_ms": started_ms,
                    "reason": "process_restart",
                    "n_missing": None,
                },
            )

    collector = Collector(con, now_ms=started_ms)

    with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as gc:
        tokens, slugs = _discover_crypto(gc)
    _register_markets(collector, tokens, slugs, start_ms=started_ms)
    collector.subscribed_tokens = list(tokens)

    store.start_session(
        con,
        session_id=session_id,
        git_commit=git_head(),
        markets_subscribed=len(tokens),
        now_ms=started_ms,
    )
    handler = WSHandler(collector, tokens=tokens, market_slugs=slugs)
    print(
        f"[collector] session {session_id}; токенов: {len(tokens)}; "
        f"бд: {db_path}; минуты: {minutes}",
        flush=True,
    )

    exit_reason = "user_stop"
    try:
        await _run_connection(handler, collector, deadline=deadline, export_root=export_root)
    except KeyboardInterrupt:
        exit_reason = "user_stop"
    except Exception as exc:  # noqa: BLE001 — процесс должен пережить любую ошибку
        exit_reason = f"error: {type(exc).__name__}: {exc}"
        log.exception("коллектор упал")
    finally:
        now = utc_ms()
        store.end_session(con, session_id=session_id, exit_reason=exit_reason, now_ms=now)
        store.export_tables(con, export_root)
        print(f"[collector] завершение: {exit_reason}", flush=True)
        print(f"[collector] stats: {json.dumps(collector.stats)}", flush=True)
        print(f"[collector] heartbeat: {json.dumps(collector.heartbeat)}", flush=True)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WS-коллектор снимков стакана Polymarket")
    p.add_argument("--minutes", type=float, default=15.0, help="длительность прогона, мин")
    p.add_argument("--vertical", default="crypto", help="вертикаль (crypto)")
    p.add_argument("--db", default=str(store.DEFAULT_DB_PATH), help="путь к duckdb")
    p.add_argument("--export-dir", default=str(store.DEFAULT_EXPORT_ROOT), help="parquet-экспорт")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(
        run(
            minutes=args.minutes,
            db_path=Path(args.db),
            export_root=Path(args.export_dir),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
