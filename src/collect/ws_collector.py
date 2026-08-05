"""WS-коллектор снимков стакана Polymarket.

Подписывается на рыночный WS CLOB, пишет book_snapshots / tick_changes /
gap_intervals / recon_checks в duckdb (data/pm.duckdb) и экспортирует
parquet-проекцию для сверки.

Запись НЕ в цикле событий: приём только кладёт строки в очередь
(store.StoreWriter), отдельный поток пишет пачками в транзакции.
Это убирает duckdb из цикла событий — причину обрывов 1011
'keepalive ping timeout' при бурстах (см. PROBE_RESULTS.md, Задача B).

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

STEP 3 (запасной план, --conns N): если обрывы остаются на тех же
~90 секундах при n_conns=1, рынки делятся на несколько параллельных
соединений ПО РЫНКАМ через _partition_market (оба токена одного рынка
на одном соединении; разбиение по токенам не используется, см. разгадку
28.6% в PROBE_RESULTS.md). Раскладка последовательная по отсортированным
slug, максимум MARKETS_PER_CONN рынков на соединение.

Запуск:  python -m src.collect.ws_collector --minutes 15 --vertical crypto
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import subprocess
import threading
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

# Допустимые вертикали. Неизвестная вертикаль -- жёсткая ошибка, а не
# молчаливый fallback на crypto (баг молчаливого игнорирования --vertical).
VALID_VERTICALS = frozenset({"crypto", "tennis"})
# STEP 3: потолок РЫНКОВ на одно соединение для АВТО-выбора числа соединений
# (n_conns=0). Деление ВСЕГДА по рынкам: оба токена одного рынка на одном
# соединении. Разбиение по токенам НЕ используется (дубль дельт).
# Измеренный серверный потолок: 70 токенов отваливаются ~77 c, 60 и 56
# выживают 191 c; 25 рынков = 50 токенов дают запас под порог 56.
MARKETS_PER_CONN = 25

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
# Сторож живости (рядом с порогами тишины): зависание 03.08 01:00:25 держало
# процесс живым 7 часов (CPU 1.39 ядра) при формально живом process —
# внешний рестарт не срабатывал. Сторож в ОТДЕЛЬНОМ потоке (не задача цикла
# событий: тот самый завис вместе с приёмом) завершает процесс ненулевым
# кодом, чтобы tennis_daemon.ps1 перезапустил коллектор.
LIVENESS_CHECK_S = 30.0  # интервал проверки сторожем
LIVENESS_STALL_S = 120.0  # ни сообщения, ни снимки дольше этого = зависание
LIVENESS_EXIT_CODE = 3  # код выхода по живости (внешний демон ждёт ненулевой)
MARKET_RECHECK_S = 60.0


def _rediscovery_due(now: float, last_recheck: float) -> bool:
    if os.environ.get("PM_DISCOVER_ONCE") == "1":
        return False
    return now - last_recheck >= MARKET_RECHECK_S


# Default берётся из EXPORT_INTERVAL_S, определённой ниже, в момент вызова.
def _periodic_export_due(now: float, last_export: float) -> bool:
    raw = os.environ.get("PM_EXPORT_INTERVAL_S")
    interval = EXPORT_INTERVAL_S if raw is None else float(raw)
    if interval < 0.0:
        raise ValueError("PM_EXPORT_INTERVAL_S must be >= 0")
    if interval == 0.0:
        return False
    return now - last_export >= interval


EXPORT_INTERVAL_S = 60.0
STATS_INTERVAL_S = 5.0
MAX_DEDUP = 20000
# Отрицательный контроль детектора потерь (recon_checks): --drop-rate выбрасывает
# долю price_change ДО применения к книге. Зерно фиксированное — прогон
# воспроизводим. Только для тестового прогона (--minutes > 0).
DROP_SEED = 20260802
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
    hash: str | None
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
    hash: str | None
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
    transaction_hash: str | None
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
                    hash=payload.get("hash"),
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
                        hash=e.get("hash"),
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
                    transaction_hash=payload.get("transaction_hash"),
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


def _install_fault_watchdog(dump_after_s: float) -> None:
    """Временная диагностика: дамп всех потоков через N секунд в stderr."""
    import faulthandler
    import sys

    def _dump() -> None:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

    threading.Timer(dump_after_s, _dump).start()


class Collector:
    """Приёмник WS-сообщений: LiveBook, строки в duckdb, гэпы, статистика.

    Сетевой части не содержит (тестируется без сети): события подаются через
    handle_event, разрывы — через record_disconnect.

    Запись идёт через writer (StoreWriter — поток-писатель в проде,
    SyncWriter — синхронно в тестах). Цикл событий не трогает duckdb.
    """

    def __init__(
        self,
        con: Any = None,
        *,
        writer: Any = None,
        now_ms: int,
        drop_rate: float = 0.0,
    ) -> None:
        # Отрицательный контроль: drop_rate>0 выбрасывает долю price_change
        # до применения к книге (фиксированное зерно). Прогон воспроизводим.
        if not (0.0 <= drop_rate <= 1.0):
            raise ValueError(f"drop_rate вне [0, 1]: {drop_rate!r}")
        if writer is None:
            writer = store.SyncWriter(con)
        self.writer = writer
        self.livebooks: dict[str, LiveBook] = {}
        self._seq_snaps: dict[str, int] = {}
        self._seq_ticks: dict[str, int] = {}
        self._dedup: deque[str] = deque()
        self._dedup_set: set[str] = set()
        self._dedup_skipped_by_token: dict[str, int] = {}
        self._dedup_skipped_queued = 0
        self._dedup_skipped_write_disabled = False
        self.pending_resync: set[str] = set()
        self._resync_from: dict[str, int] = {}
        self._silence_open = False
        self.subscribed_tokens: list[str] = []
        self.last_activity_ms = now_ms
        self.drop_rate = float(drop_rate)
        self._rng = random.Random(DROP_SEED)
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
            "dropped": 0,
        }
        self.heartbeat = {
            "ping_interval_s": PING_INTERVAL_S,
            "max_silence_s": 0.0,
            "n_silence_episodes": 0,
            "n_pings_fired": 0,
        }
        # Per-connection статистика (приёмка мультисоединённого транспорта,
        # Задача 2): ключ conn_id (0-based индекс соединения), значения —
        # счётчики ОДНОГО соединения, не общие. Заполняется по мере приёма,
        # выгружается в conn_stats в конце run(). Первым инициализирует
        # _run_connection через _conn_stats(conn_id) с n_tokens.
        self.conn_stats: dict[int, dict[str, int | float | None]] = {}

    def _conn_stats(self, conn_id: int) -> dict[str, int | float | None]:
        """Счётчики одного соединения (ленивая инициализация)."""
        st = self.conn_stats.get(conn_id)
        if st is None:
            st = {
                "n_tokens": 0,
                "messages": 0,
                "events": 0,
                "recons": 0,
                "recons_mismatch": 0,
                "reconnects": 0,
                "max_silence_s": 0.0,
                "n_silence_episodes": 0,
                "n_pings_fired": 0,
                "first_msg_ms": None,
                "last_msg_ms": None,
            }
            self.conn_stats[conn_id] = st
        return st

    def _conn_messages(self, conn_id: int, ts_recv_ms: int) -> None:
        """Сообщение принято соединением conn_id: счётчики + first/last."""
        st = self._conn_stats(conn_id)
        st["messages"] = int(st["messages"]) + 1
        if st["first_msg_ms"] is None:
            st["first_msg_ms"] = ts_recv_ms
        st["last_msg_ms"] = ts_recv_ms

    def prewarm_seq(self, token_ids: Sequence[str]) -> None:
        """Предзаполнить счётчики seq для токенов ДО цикла приёма.

        Ленивый writer.call() в _seq_snap/_seq_tick блокировал бы цикл
        событий на первом событии токена (при бурстах — срыв keepalive).
        Один батч-запрос до подписки снимает все чтения из цикла приёма.
        Уже активные счётчики (in-memory выше базы) НЕ трогаем.
        """
        missing = [
            t
            for t in token_ids
            if t not in self._seq_snaps or t not in self._seq_ticks
        ]
        if not missing:
            return
        snaps = self.writer.call(store.current_seqs, "book_snapshots", missing)
        ticks = self.writer.call(store.current_seqs, "tick_changes", missing)
        for t in missing:
            if t not in self._seq_snaps:
                self._seq_snaps[t] = snaps.get(t, 1)
            if t not in self._seq_ticks:
                self._seq_ticks[t] = ticks.get(t, 1)

    def _seq_snap(self, token_id: str) -> int:
        if token_id not in self._seq_snaps:
            self._seq_snaps[token_id] = self.writer.call(
                store.current_seq, "book_snapshots", token_id
            )
        value = self._seq_snaps[token_id]
        self._seq_snaps[token_id] += 1
        return value

    def _seq_tick(self, token_id: str) -> int:
        if token_id not in self._seq_ticks:
            self._seq_ticks[token_id] = self.writer.call(
                store.current_seq, "tick_changes", token_id
            )
        value = self._seq_ticks[token_id]
        self._seq_ticks[token_id] += 1
        return value

    def _record_dedup_skip(self, event: Event, ts_recv_ms: int) -> None:
        token_id = event.token_id
        self._dedup_skipped_by_token[token_id] = self._dedup_skipped_by_token.get(token_id, 0) + 1
        if self._dedup_skipped_write_disabled:
            return
        if self._dedup_skipped_queued >= 200000:
            self._dedup_skipped_write_disabled = True
            log.warning("dedup_skipped limit reached; further skipped events are not written")
            return
        side = getattr(event, "side", None)
        self._dedup_skipped_queued += 1
        self.writer.submit_row("dedup_skipped", {
            "ts_recv_ms": ts_recv_ms,
            "token_id": token_id,
            "side": side,
            "price": event.price,
            "size": event.size,
            "hash": getattr(event, "hash", None) or getattr(event, "transaction_hash", None),
            "reason": "duplicate",
        })

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
        """Ключ анти-дубля зависит от ТИПА сообщения (серверные поля, не md5).

        Collector ОДИН на все соединения (общий writer, общий _dedup_set),
        поэтому дедуп идёт ГЛОБАЛЬНО поверх всех соединений: сервер шлёт на
        подписку одним токеном и его комплемент, и при мультисоединении
        каждая дельта приходит несколько раз — ключ снимает все, кроме
        первой, независимо от того, через какое соединение она пришла.

        Правило (решение владельца 2026-08-03, см. DECISIONS_NEEDED.md):
        - book          -> (asset_id, hash)          # hash сообщения
        - price_change  -> (asset_id, hash, price, size)
          hash элемента price_changes НЕ уникален: сервер переиспользует его
          для разных изменений одного актива в пределах миллисекунды
          (logs/ws_raw.jsonl, recv_ms=1785651207744: price=0.3/size=8700 против
          price=0.4/size=6781.44 с одним hash), поэтому price и size обязаны
          входить в ключ — иначе легитимная дельта отбрасывается (ASSUMPTIONS.md).
        - last_trade_price -> (asset_id, transaction_hash, price, size)
          одна транзакция может нести несколько исполнений по активу, и ключ
          только по (asset_id, transaction_hash) склеил бы разные сделки.
        Тип без hash и без transaction_hash -> ValueError с event_type в тексте
        (не дропать и не подставлять заглушку).
        """
        if isinstance(event, BookEvent):
            return self._key_by_hash(event.token_id, event.hash, "book")
        if isinstance(event, DeltaEvent):
            if not event.hash:
                raise ValueError(
                    f"тип 'price_change' без hash (token_id="
                    f"{event.token_id!r}): дедуп невозможен"
                )
            return (
                f"{event.token_id}|{event.hash}|"
                f"{event.price}|{event.size}"
            )
        if isinstance(event, TradeEvent):
            if not event.transaction_hash:
                raise ValueError(
                    f"тип 'last_trade_price' без transaction_hash (token_id="
                    f"{event.token_id!r}): дедуп невозможен"
                )
            return (
                f"{event.token_id}|{event.transaction_hash}|"
                f"{event.price}|{event.size}"
            )
        raise ValueError(
            f"неизвестный тип события {type(event).__name__}: нет ни hash, "
            "ни transaction_hash"
        )

    @staticmethod
    def _key_by_hash(token_id: str, value: str | None, event_type: str) -> str:
        if not value:
            raise ValueError(
                f"тип {event_type!r} без hash (token_id={token_id!r}): "
                "дедуп невозможен"
            )
        return f"{token_id}|{value}"

    def _livebook(self, token_id: str) -> LiveBook:
        book = self.livebooks.get(token_id)
        if book is None:
            book = LiveBook()
            self.livebooks[token_id] = book
        return book

    def handle_event(self, event: Event, ts_recv_ms: int, conn_id: int = 0) -> None:
        """Применить событие: обновить LiveBook и записать строки.

        conn_id — номер соединения, принявшего событие (для per-connection
        статистики приёмки, Задача 2). По умолчанию 0: тесты и одиночное
        соединение не обязаны передавать.
        """
        self.stats["events"] += 1
        st = self._conn_stats(conn_id)
        st["events"] = int(st["events"]) + 1
        self.last_activity_ms = ts_recv_ms

        if isinstance(event, BookEvent):
            self._handle_book(event, ts_recv_ms, conn_id=conn_id)
        elif isinstance(event, DeltaEvent):
            self._handle_delta(event, ts_recv_ms, conn_id=conn_id)
        elif isinstance(event, TradeEvent):
            self._handle_trade(event, ts_recv_ms, conn_id=conn_id)

    def _handle_book(self, event: BookEvent, ts_recv_ms: int, conn_id: int = 0) -> None:
        if self._is_duplicate(self._dedup_key(event)):
            self._record_dedup_skip(event, ts_recv_ms)
            return
        st = self._conn_stats(conn_id)
        live = self._livebook(event.token_id)
        seq = self._seq_snap(event.token_id)
        self.writer.submit_row(
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
            seq=seq,
            ours=live,
            theirs_bids=theirs_bids,
            theirs_asks=theirs_asks,
            n_skipped_dedup_token=self._dedup_skipped_by_token.get(event.token_id, 0),
        )
        self.writer.submit_row("recon_checks", rc)
        self.stats["recons"] += 1
        st["recons"] = int(st["recons"]) + 1
        if rc["verdict"] == "mismatch":
            self.stats["recons_mismatch"] += 1
            st["recons_mismatch"] = int(st["recons_mismatch"]) + 1

        if event.token_id in self.pending_resync:
            self.writer.submit_row(
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

        self.writer.submit_row(
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

    def _handle_delta(self, event: DeltaEvent, ts_recv_ms: int, conn_id: int = 0) -> None:
        live = self._livebook(event.token_id)
        if event.side not in (SIDE_BUY, SIDE_SELL):
            self.stats["unknown_types"] += 1
            return
        if self._is_duplicate(self._dedup_key(event)):
            self._record_dedup_skip(event, ts_recv_ms)
            return
        # Отрицательный контроль: выбрасываем долю price_change ДО применения
        # к книге — книга расходится с сервером, recon_checks обязан поймать.
        if self.drop_rate > 0.0 and self._rng.random() < self.drop_rate:
            self.stats["dropped"] += 1
            return
        live.apply_change(event.side, event.price, event.size)

        seq = self._seq_snap(event.token_id)
        self.writer.submit_row(
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

        self.writer.submit_row(
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

    def _handle_trade(self, event: TradeEvent, ts_recv_ms: int, conn_id: int = 0) -> None:
        if self._is_duplicate(self._dedup_key(event)):
            self._record_dedup_skip(event, ts_recv_ms)
            return
        self.writer.submit_row(
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

    def observe_silence(
        self,
        now_ms: int,
        tokens: Sequence[str] | None = None,
        *,
        from_ms: int | None = None,
        conn_id: int = 0,
    ) -> None:
        """Мягкий флаг тишины: молчание дольше порога — time_gap, не разрыв.

        tokens — те токены, чей канал замолчал (при мультисоединении —
        только своё соединение, не все подписки). from_ms — начало окна
        тишины этого соединения (при мультисоединении своё, не глобальное).
        conn_id — номер соединения для per-connection статистики (Задача 2).
        """
        if tokens is None:
            tokens = self.subscribed_tokens
        if from_ms is None:
            from_ms = self.last_activity_ms
        idle_s = (now_ms - from_ms) / 1000.0
        self.heartbeat["max_silence_s"] = max(self.heartbeat["max_silence_s"], idle_s)
        st = self._conn_stats(conn_id)
        st["max_silence_s"] = max(float(st["max_silence_s"]), idle_s)
        if idle_s >= PING_INTERVAL_S:
            self.heartbeat["n_pings_fired"] += 1
            st["n_pings_fired"] = int(st["n_pings_fired"]) + 1
        if idle_s >= SILENCE_THRESHOLD_S:
            if not self._silence_open:
                self._silence_open = True
                self.heartbeat["n_silence_episodes"] += 1
                st["n_silence_episodes"] = int(st["n_silence_episodes"]) + 1
                for token_id in tokens:
                    self.writer.submit_row(
                        "gap_intervals",
                        {
                            "token_id": token_id,
                            "start_ms": from_ms,
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
        conn_id: int = 0,
    ) -> None:
        """Обрыв соединения: gap_intervals(reason=disconnect) по всем токенам."""
        self.stats["reconnects"] += 1
        st = self._conn_stats(conn_id)
        st["reconnects"] = int(st["reconnects"]) + 1
        for token_id in tokens:
            self.writer.submit_row(
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


def _iso_ms(ms: int) -> str:
    """epoch ms -> 'YYYY-MM-DD HH:MM:SS' UTC (для строки сторожа живости)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000.0))


def _exit_process(code: int) -> None:
    """Безусловный выход процесса: мимо finally и writer-потока."""
    os._exit(code)


class LivenessWatchdog:
    """Сторож живости коллектора (задача 2026-08-04).

    Отдельный поток (не задача цикла событий): зависание 03.08 01:00:25
    заморозило цикл событий вместе с приёмом (последний stats 01:00:25,
    дальше 7 часов процесс жил на CPU) — asyncio-задача не сработала бы.

    Правило: если НИ счётчик принятых сообщений, НИ счётчик записанных
    снимков не выросли за LIVENESS_STALL_S — пишет в лог оба счётчика,
    время последнего роста и причину, затем завершает процесс кодом
    LIVENESS_EXIT_CODE (ненулевым), чтобы tennis_daemon.ps1 перезапустил.

    Штатное завершение по --minutes НЕ срабатывает: run() зовёт stop()
    в начале finally, поток просыпается от события и выходит без вызова
    exit_fn.
    """

    def __init__(
        self,
        collector: Collector,
        *,
        check_interval_s: float = LIVENESS_CHECK_S,
        stall_s: float = LIVENESS_STALL_S,
        exit_fn: Any = None,
    ) -> None:
        self._collector = collector
        self._check_s = check_interval_s
        self._stall_s = stall_s
        self._exit_fn = exit_fn if exit_fn is not None else _exit_process
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="liveness-watchdog", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Штатная остановка (--minutes): сторож выходит без срабатывания."""
        self._stop.set()
        self._thread.join(timeout=self._check_s + 1.0)

    def _run(self) -> None:
        last_growth_ms = utc_ms()
        last_messages = int(self._collector.stats["messages"])
        last_snapshots = int(self._collector.stats["snapshots"])
        while not self._stop.wait(self._check_s):
            now_ms = utc_ms()
            messages = int(self._collector.stats["messages"])
            snapshots = int(self._collector.stats["snapshots"])
            if messages != last_messages or snapshots != last_snapshots:
                last_growth_ms = now_ms
                last_messages = messages
                last_snapshots = snapshots
                continue
            stalled_ms = now_ms - last_growth_ms
            if stalled_ms >= self._stall_s * 1000:
                reason = (
                    f"ни приём сообщений, ни запись снимков не росли "
                    f"{stalled_ms / 1000.0:.1f} с подряд (лимит {self._stall_s:.1f} с)"
                )
                log.error(
                    "СТОП живости: %s messages=%d snapshots=%d "
                    "last_growth_ms=%d last_growth=%s",
                    reason,
                    messages,
                    snapshots,
                    last_growth_ms,
                    _iso_ms(last_growth_ms),
                )
                self._exit_fn(LIVENESS_EXIT_CODE)
                return


class WSHandler:
    """Сетевой слой: разбор сообщений и запись в collector."""

    def __init__(
        self,
        collector: Collector,
        *,
        tokens: list[str],
        market_slugs: dict[str, str],
        conn_id: int = 0,
    ) -> None:
        self.collector = collector
        self.tokens = list(tokens)
        self.market_slugs = dict(market_slugs)
        self.conn_id = conn_id
        self._market_id_written: set[str] = set()
        self.last_activity_ms: int | None = None

    def handle_message(self, raw: str, ts_recv_ms: int) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        self.collector.stats["messages"] += 1
        self.collector._conn_messages(self.conn_id, ts_recv_ms)
        self.last_activity_ms = ts_recv_ms
        try:
            payload = json.loads(raw)
        except ValueError:
            self.collector.stats["unknown_types"] += 1
            return
        events, markets = interpret_message(payload, ts_recv_ms)
        for token_id, market_id in markets.items():
            if market_id and token_id not in self._market_id_written:
                self.collector.writer.submit_call(
                    store.update_market_field, token_id, market_id=market_id
                )
                self._market_id_written.add(token_id)
        for event in events:
            self.collector.handle_event(event, ts_recv_ms, conn_id=self.conn_id)
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


def _discover_tennis(client: httpx.Client) -> tuple[list[str], dict[str, str]]:
    """Матчевые winner-рынки тенниса (одиночки) через src.validate.discovery."""
    from src.validate.discovery import tennis_matches

    res = tennis_matches(client)
    tokens: list[str] = []
    slugs: dict[str, str] = {}
    for m in res.matches:
        for token_id in m.token_ids:
            tokens.append(token_id)
            slugs[token_id] = m.market_slug
    return tokens, slugs


def _discover(vertical: str, client: httpx.Client) -> tuple[list[str], dict[str, str]]:
    """Discovery по вертикали. Неизвестная вертикаль -> ValueError."""
    if vertical == "crypto":
        return _discover_crypto(client)
    if vertical == "tennis":
        return _discover_tennis(client)
    raise ValueError(f"неизвестная вертикаль: {vertical!r}; допустимо: {sorted(VALID_VERTICALS)}")


BACKFILL_CONCURRENCY = 16


def _partition_market(market_key: str, markets_sorted: Sequence[str], n_conns: int) -> int:
    """Стабильное разбиение РЫНКОВ по соединениям.

    Ключ — market_key (слаг рынка, общий для обоих токенов одного рынка),
    а НЕ token_id. Оба токена одного рынка обязаны попасть на одно соединение:
    иначе сервер (он шлёт на подписку одним токеном и его комплемент) отдаёт
    каждое событие ДВАЖДЫ — по разу на каждое из двух соединений, и книга
    уезжает (см. PROBE_RESULTS.md, разгадка 28.6%).

    Раскладка ПОСЛЕДОВАТЕЛЬНАЯ: рынки сортируются по slug (markets_sorted),
    занимают корзину bin = rank // MARKETS_PER_CONN. Максимум MARKETS_PER_CONN
    рынков (50 токенов) на соединение — запас под измеренный серверный потолок
    (56 токенов жили 191 c, 70 отвалились ~77 c). Раскладка детерминирована и
    стабильна между переоткрытиями соединений и re-discovery. n_conns <= 1 ->
    всегда 0.
    """
    if n_conns <= 1:
        return 0
    rank = markets_sorted.index(market_key)
    return min(rank // MARKETS_PER_CONN, n_conns - 1)


def _partition_tokens(
    tokens: Sequence[str], market_of: dict[str, str], n_conns: int
) -> list[list[str]]:
    """tokens -> n_conns списков, разбитых ПО РЫНКАМ (не по токенам).

    Группировка: токены одного рынка (общий market_of[token_id]) попадают на
    одно соединение. Рынки сортируются по slug и раскладываются последовательно
    (bin = rank // MARKETS_PER_CONN), не более MARKETS_PER_CONN рынков на
    соединение. Порядок токенов на соединении стабилен, но не гарантирован
    по исходному списку — важно только то, что рынок неделим и потолок 50
    токенов не превышен.
    """
    by_market: dict[str, list[str]] = {}
    for t in tokens:
        by_market.setdefault(market_of.get(t, t), []).append(t)
    markets_sorted = sorted(by_market)
    parts: list[list[str]] = [[] for _ in range(n_conns)]
    for mkey, members in by_market.items():
        parts[_partition_market(mkey, markets_sorted, n_conns)].extend(members)
    return parts


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
        collector.writer.submit_row(
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
    vertical: str,
) -> None:
    for token_id in tokens:
        collector.writer.submit_call(
            store.upsert_market,
            {
                "token_id": token_id,
                "market_id": None,
                "event_id": slugs.get(token_id),
                "vertical": vertical,
                "start_ms": start_ms,
                "end_ms": None,
                "resolved": False,
            },
        )


async def _resubscribe(
    handler: WSHandler,
    collector: Collector,
    *,
    n_conns: int = 1,
    partition: int = 0,
    vertical: str = "crypto",
    fixed_tokens: Sequence[str] | None = None,
    fixed_slugs: dict[str, str] | None = None,
) -> bool:
    """Периодический re-discovery: новые токены -> markets_tracked + подписка.

    Discovery уходит в поток (asyncio.to_thread): синхронный httpx в цикле
    событий блокировал бы ответ на keepalive-пинг сервера.

    При мультисоединении (n_conns > 1) соединение подписывается ТОЛЬКО на
    свои токены (разбиение ПО РЫНКАМ через _partition_market): один рынок
    (оба его токена) ровно на одном соединении.

    fixed_tokens/fixed_slugs — фиксированный набор вместо discovery
    (приёмка Задачи 2: оба прогона на одном наборе рынков). Новые рынки
    за пределами фиксированного набора НЕ добавляются.
    """
    try:
        if fixed_tokens is not None:
            tokens = list(fixed_tokens)
            slugs = dict(fixed_slugs or {})
        else:
            tokens, slugs = await asyncio.to_thread(_discover_with_client, vertical)
    except Exception as exc:  # noqa: BLE001
        log.warning("re-discovery не удался: %r", exc)
        return False
    markets_sorted = sorted({slugs.get(t, t) for t in tokens})
    mine = [
        t
        for t in tokens
        if _partition_market(slugs.get(t, t), markets_sorted, n_conns) == partition
    ]
    known = set(handler.tokens)
    new = [t for t in mine if t not in known]
    if not new:
        return False
    _register_markets(collector, new, slugs, start_ms=utc_ms(), vertical=vertical)
    handler.tokens = mine
    collector.subscribed_tokens = list(tokens)
    collector.prewarm_seq(new)
    log.info(
        "re-discovery (соед. %d/%d): новых %d, своих %d, всего %d",
        partition + 1,
        n_conns,
        len(new),
        len(mine),
        len(tokens),
    )
    return True


def _discover_with_client(vertical: str = "crypto") -> tuple[list[str], dict[str, str]]:
    """Синхронный discovery на собственном httpx.Client (для to_thread)."""
    with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as gc:
        return _discover(vertical, gc)


async def _run_connection(
    handler: WSHandler,
    collector: Collector,
    *,
    deadline: int,
    export_root: Path,
    n_conns: int = 1,
    partition: int = 0,
    vertical: str = "crypto",
    fixed_tokens: Sequence[str] | None = None,
    fixed_slugs: dict[str, str] | None = None,
) -> None:
    """Одна стабильная WS-сессия: подключение, приём, ротация, экспорт.
    Переподключение с экспоненциальной задержкой внутри. Выход по deadline."""
    st = collector._conn_stats(handler.conn_id)
    st["n_tokens"] = len(handler.tokens)
    delay = RECONNECT_BASE_S
    last_recheck = time.monotonic()
    last_export = time.monotonic()
    last_stats = time.monotonic()
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
                log.info(
                    "подключено (соед. %d/%d); подписка %d токенов",
                    partition + 1,
                    n_conns,
                    len(handler.tokens),
                )
                await ws.send(json.dumps({"type": "market", "assets_ids": handler.tokens}))
                collector.prewarm_seq(handler.tokens)
                await _rest_backfill(collector, handler.tokens, ts_recv_ms=utc_ms())
                while True:
                    now_wall = utc_ms()
                    if now_wall >= deadline:
                        return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        collector.observe_silence(
                            utc_ms(), tokens=handler.tokens,
                            from_ms=handler.last_activity_ms, conn_id=handler.conn_id,
                        )
                        continue
                    handler.handle_message(raw, utc_ms())
                    # Бар: обработка сообщения синхронна (разбор + очередь).
                    # Явный yield даёт циклу событий обслужить управляющие кадры
                    # (pong на пинг сервера); duckdb не трогается вообще —
                    # запись в отдельном потоке-писателе.
                    await asyncio.sleep(0)

                    now = time.monotonic()
                    if _rediscovery_due(now, last_recheck):
                        last_recheck = now
                        if await _resubscribe(
                            handler, collector, n_conns=n_conns, partition=partition,
                            vertical=vertical, fixed_tokens=fixed_tokens,
                            fixed_slugs=fixed_slugs,
                        ):
                            await ws.send(
                                json.dumps({"type": "market", "assets_ids": handler.tokens})
                            )
                    if _periodic_export_due(now, last_export):
                        last_export = now
                        collector.writer.submit_call(store.export_tables, export_root)
                    if now - last_stats >= STATS_INTERVAL_S:
                        last_stats = now
                        log.info(
                            "stats: %s | msgs_total=%d snap=%d gap=%d",
                            json.dumps(collector.stats, ensure_ascii=False),
                            collector.stats["messages"],
                            collector.stats["snapshots"],
                            collector.stats["reconnects"],
                        )
        except websockets.ConnectionClosed as exc:
            log.warning("соединение закрыто: %r", exc)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log.warning("ошибка сокета: %r", exc)
        if utc_ms() >= deadline:
            return
        collector.record_disconnect(
            handler.tokens,
            ts_from_ms=(
                handler.last_activity_ms
                if handler.last_activity_ms is not None
                else collector.last_activity_ms
            ),
            ts_to_ms=utc_ms(),
            conn_id=handler.conn_id,
        )


async def run(
    *,
    minutes: float,
    db_path: Path,
    export_root: Path,
    drop_rate: float = 0.0,
    n_conns: int = 1,
    vertical: str = "crypto",
    fixed_tokens: Sequence[str] | None = None,
    fixed_slugs: dict[str, str] | None = None,
) -> int:
    """Основной цикл: сессия, discovery, приём, гэпы, экспорт.

    n_conns > 1 — STEP 3 (запасной план): рынки делятся ПО РЫНКАМ
    (оба токена одного рынка на одном соединении), не по токенам.
    n_conns == 0 — авто: число соединений = ceil(число рынков /
    MARKETS_PER_CONN), минимум 1 (потолок 50 токенов на соединение).

    fixed_tokens/fixed_slugs — фиксированный набор рынков вместо discovery
    (приёмка Задачи 2: два прогона подряд на ОДНОМ наборе рынков — эталон
    односоединённый, затем многосоединённый). Re-discovery при фиксированном
    наборе использует тот же набор и не добавляет новых рынков.
    """
    if vertical not in VALID_VERTICALS:
        raise ValueError(
            f"неизвестная вертикаль: {vertical!r}; допустимо: {sorted(VALID_VERTICALS)}"
        )
    writer = store.StoreWriter(db_path)
    session_id = uuid.uuid4().hex[:12]
    started_ms = utc_ms()
    deadline = started_ms + int(minutes * 60_000)

    # process_restart: были прошлые сессии -> документируем простой по токенам
    prev_start, prev_end = writer.call(store.last_session_bounds)
    if prev_start is not None:
        from_ts = prev_end if prev_end is not None else prev_start
        for token_id in writer.call(store.tracked_tokens):
            writer.submit_row(
                "gap_intervals",
                {
                    "token_id": token_id,
                    "start_ms": from_ts,
                    "end_ms": started_ms,
                    "reason": "process_restart",
                    "n_missing": None,
                },
            )

    collector = Collector(writer=writer, now_ms=started_ms, drop_rate=drop_rate)

    with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as gc:
        if fixed_tokens is not None:
            tokens = list(fixed_tokens)
            slugs = dict(fixed_slugs or {})
        else:
            tokens, slugs = _discover(vertical, gc)
    if n_conns == 0:
        n_markets = len({slugs.get(t, t) for t in tokens})
        n_conns = max(1, (n_markets + MARKETS_PER_CONN - 1) // MARKETS_PER_CONN)
    _register_markets(collector, tokens, slugs, start_ms=started_ms, vertical=vertical)
    collector.subscribed_tokens = list(tokens)

    writer.submit_call(
        store.start_session,
        session_id=session_id,
        git_commit=git_head(),
        markets_subscribed=len(tokens),
        now_ms=started_ms,
    )
    print(
        f"[collector] session {session_id}; вертикаль: {vertical}; токенов: {len(tokens)}; "
        f"соединений: {n_conns}; бд: {db_path}; минуты: {minutes}",
        flush=True,
    )

    watchdog = LivenessWatchdog(collector)
    watchdog.start()

    exit_reason = "user_stop"
    try:
        if n_conns > 1:
            parts = _partition_tokens(tokens, slugs, n_conns)
            for i, part in enumerate(parts):
                print(
                    f"[collector] соединение {i + 1}/{n_conns}: токенов {len(part)}",
                    flush=True,
                )
            tasks = [
                _run_connection(
                    WSHandler(collector, tokens=part, market_slugs=slugs, conn_id=i),
                    collector,
                    deadline=deadline,
                    export_root=export_root,
                    n_conns=n_conns,
                    partition=i,
                    vertical=vertical,
                    fixed_tokens=fixed_tokens,
                    fixed_slugs=fixed_slugs,
                )
                for i, part in enumerate(parts)
            ]
            await asyncio.gather(*tasks)
        else:
            handler = WSHandler(collector, tokens=tokens, market_slugs=slugs, conn_id=0)
            await _run_connection(
                handler, collector, deadline=deadline, export_root=export_root,
                vertical=vertical, fixed_tokens=fixed_tokens,
                fixed_slugs=fixed_slugs,
            )
    except KeyboardInterrupt:
        exit_reason = "user_stop"
    except Exception as exc:  # noqa: BLE001 — процесс должен пережить любую ошибку
        exit_reason = f"error: {type(exc).__name__}: {exc}"
        log.exception("коллектор упал")
    finally:
        # Сторож останавливается ДО финальной записи/экспорта: при штатном
        # завершении (--minutes, KeyboardInterrupt) тихая пауза не должна
        # превращаться в срабатывание живости. При ЗАВИСАНИИ сюда не дойдём —
        # сторож сам завершит процесс ненулевым кодом.
        watchdog.stop()
        now = utc_ms()
        try:
            for conn_id in sorted(collector.conn_stats):
                st = collector.conn_stats[conn_id]
                writer.submit_row(
                    "conn_stats",
                    {
                        "session_id": session_id,
                        "conn_id": conn_id,
                        "n_tokens": int(st["n_tokens"]),
                        "messages": int(st["messages"]),
                        "events": int(st["events"]),
                        "recons": int(st["recons"]),
                        "recons_mismatch": int(st["recons_mismatch"]),
                        "reconnects": int(st["reconnects"]),
                        "max_silence_s": round(float(st["max_silence_s"]), 3),
                        "n_silence_episodes": int(st["n_silence_episodes"]),
                        "n_pings_fired": int(st["n_pings_fired"]),
                        "first_msg_ms": st["first_msg_ms"],
                        "last_msg_ms": st["last_msg_ms"],
                    },
                )
            writer.call(
                store.end_session,
                session_id=session_id,
                exit_reason=exit_reason,
                now_ms=now,
            )
            writer.call(store.export_tables, export_root)
        finally:
            writer.close()
        print(f"[collector] завершение: {exit_reason}", flush=True)
        print(f"[collector] stats: {json.dumps(collector.stats)}", flush=True)
        print(f"[collector] heartbeat: {json.dumps(collector.heartbeat)}", flush=True)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WS-коллектор снимков стакана Polymarket")
    p.add_argument("--minutes", type=float, default=15.0, help="длительность прогона, мин")
    p.add_argument(
        "--vertical",
        default="crypto",
        choices=sorted(VALID_VERTICALS),
        help=f"вертикаль ({', '.join(sorted(VALID_VERTICALS))})",
    )
    p.add_argument("--db", default=str(store.DEFAULT_DB_PATH), help="путь к duckdb")
    p.add_argument("--export-dir", default=str(store.DEFAULT_EXPORT_ROOT), help="parquet-экспорт")
    p.add_argument(
        "--drop-rate",
        type=float,
        default=0.0,
        help="отрицательный контроль: доля выбрасываемых price_change до применения "
        "к книге (0..1, фиксированное зерно). Только для тестового прогона "
        "(--minutes > 0); в рабочем режиме (--minutes 0) недоступен.",
    )
    p.add_argument(
        "--conns",
        type=int,
        default=0,
        help="число параллельных WS-соединений (STEP 3): РЫНКИ делятся "
        "по рынкам (оба токена одного рынка на одном соединении); "
        "0 = авто по числу рынков; 1 = одно соединение на все токены",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not (0.0 <= args.drop_rate <= 1.0):
        raise SystemExit(f"--drop-rate вне [0, 1]: {args.drop_rate!r}")
    if args.drop_rate > 0.0 and args.minutes == 0:
        raise SystemExit(
            "--drop-rate недоступен в рабочем режиме (--minutes 0): "
            "это тестовый отрицательный контроль, в демоне запрещён"
        )
    if args.conns < 0:
        raise SystemExit(f"--conns должно быть >= 0 (0 = авто): {args.conns!r}")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_fault_watchdog(45.0)
    return asyncio.run(
        run(
            minutes=args.minutes,
            db_path=Path(args.db),
            export_root=Path(args.export_dir),
            drop_rate=args.drop_rate,
            n_conns=args.conns,
            vertical=args.vertical,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
