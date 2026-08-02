"""Опрос REST /book -- второй независимый источник книги заявок.

Задача 2 проверочного контура: опрашивать clob.polymarket.com/book по списку
token_id раз в 5 секунд и писать parquet в ЗАМОРОЖЕННОЙ СХЕМЕ.

Замороженная схема (общий контракт с коллектором, менять нельзя):
  ts_recv_ms int64 | ts_server_ms int64 | token_id text | best_bid float64 |
  best_ask float64 | bid_size float64 | ask_size float64 | spread float64 |
  vwap_bid_100 float64 | vwap_ask_100 float64 | book_age_ms int64 | seq int64

Статус знания (свежая проба 2026-08-02):
- ФАКТ: /book отвечает 200 и содержит СЕРВЕРНУЮ метку времени
  "timestamp":"1785652861636" -- 13-значное целое, epoch MILLISECONDS
  (те же единицы, что у WS price_change).
- ФАКТ: bids -- массив {price, size} ПО ВОЗРАСТАНИЮ цены (лучший последний);
  asks -- ПО УБЫВАНИЮ (лучший последний). Поля -- строки.
- ФАКТ: книга может быть пустой/односторонней: для пустой стороны пишем NULL,
  а не выдумываем цену.
- ФАКТ: серверная метка приходит во ВСЕХ ответах (проба), но код на её
  отсутствие рассчитан: ts_server_ms/book_age_ms -> NULL, и это объявляется.
- ПРЕДПОЛОЖЕНИЕ: первых 100 контрактов хватает на стороне книги, если глубина
  стороны >= 100; при глубине < 100 vwap пишется NULL (требование задания).

Лимит: 1500 запросов / 10 c на IP. Держим с большим запасом -- не более
MAX_REQUESTS_PER_10S = 300.

seq -- локальный счётчик ОПРОСА: инкрементируется раз за цикл (полный проход
по списку токенов). Все строки одного цикла несут одно и то же seq.
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

BOOK_URL = "https://clob.polymarket.com/book"
USER_AGENT = "pm-validate/0.1 (personal research)"
POLL_INTERVAL_S = 5.0
MAX_REQUESTS_PER_10S = 300
RETRY_ATTEMPTS = 3
VWAP_QUANTITY = 100.0

# Замороженная схема. Изменение имён или типов -- нарушение контракта.
BOOK_SCHEMA = pa.schema(
    [
        pa.field("ts_recv_ms", pa.int64()),
        pa.field("ts_server_ms", pa.int64(), nullable=True),
        pa.field("token_id", pa.string()),
        pa.field("best_bid", pa.float64(), nullable=True),
        pa.field("best_ask", pa.float64(), nullable=True),
        pa.field("bid_size", pa.float64(), nullable=True),
        pa.field("ask_size", pa.float64(), nullable=True),
        pa.field("spread", pa.float64(), nullable=True),
        pa.field("vwap_bid_100", pa.float64(), nullable=True),
        pa.field("vwap_ask_100", pa.float64(), nullable=True),
        pa.field("book_age_ms", pa.int64(), nullable=True),
        pa.field("seq", pa.int64()),
    ]
)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ParsedBook:
    """Разобранный ответ /book: серверная метка и стороны книги.

    levels_bids / levels_asks -- списки пар (price, size) float, порядок как в
    ответе. Лучшая цена вычисляется по максимуму/минимуму, а не по позиции,
    чтобы не зависеть от порядка массива.
    """

    server_ts_ms: int | None
    levels_bids: tuple[tuple[float, float], ...]
    levels_asks: tuple[tuple[float, float], ...]

    @property
    def is_empty(self) -> bool:
        return not self.levels_bids and not self.levels_asks

    @property
    def is_incomplete(self) -> bool:
        return bool(self.levels_bids) != bool(self.levels_asks)


@dataclass(frozen=True)
class PollSummary:
    """Статистика прогона для отчёта."""

    rows_written: int
    unique_tokens: int
    n_empty: int
    n_incomplete: int
    n_failed: int
    n_with_server_ts: int
    cycles: int
    requests: int
    elapsed_s: float
    out_path: Path | None

    @property
    def requests_per_second(self) -> float:
        return self.requests / self.elapsed_s if self.elapsed_s > 0 else 0.0


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_levels(raw: object) -> tuple[tuple[float, float], ...]:
    """Массив {price, size} (строки или числа) -> пары float. Мусор пропускаем."""
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


def _server_ts_ms(payload: dict[str, Any]) -> int | None:
    """Серверная метка: 13-значное целое, epoch ms. Нет/не парсится -> None."""
    raw = payload.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return None


def parse_book(payload: Any) -> ParsedBook:
    """Сырой JSON /book -> ParsedBook.

    Raises:
        ValueError: если payload не словарь (ответ не похож на книгу).
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"/book вернул {type(payload).__name__}, ожидался словарь: {payload!r:.200}"
        )
    return ParsedBook(
        server_ts_ms=_server_ts_ms(payload),
        levels_bids=_parse_levels(payload.get("bids")),
        levels_asks=_parse_levels(payload.get("asks")),
    )


def vwap_first_qty(
    levels: Sequence[tuple[float, float]],
    *,
    best_first: bool,
    quantity: float = VWAP_QUANTITY,
) -> float | None:
    """Средневзвешенная цена первых `quantity` контрактов стороны.

    levels -- пары (price, size). best_first=True: уровень с ЛУЧШЕЙ ценой
    первый (для bids -- по убыванию цены, для asks -- по возрастанию).

    Возвращает None, если суммарная глубина < quantity (частичное значение
    запрещено: оно хуже отсутствующего).
    """
    if quantity <= 0:
        return None
    ordered = sorted(levels, key=lambda p: p[0], reverse=best_first)
    num = 0.0
    total = 0.0
    remaining = quantity
    for price, size in ordered:
        if size <= 0:
            continue
        take = min(remaining, size)
        num += price * take
        total += take
        remaining -= take
        if remaining <= 0:
            return num / total
    return None


def _best_level(
    levels: Sequence[tuple[float, float]], *, prefer_highest: bool
) -> tuple[float, float] | None:
    """Лучший уровень стороны: max цены для bids, min для asks."""
    if not levels:
        return None
    best = levels[0]
    for price, size in levels[1:]:
        if (prefer_highest and price > best[0]) or (
            not prefer_highest and price < best[0]
        ):
            best = (price, size)
    return best


def build_row(
    pb: ParsedBook,
    *,
    token_id: str,
    ts_recv_ms: int,
    seq: int,
) -> dict[str, Any]:
    """ParsedBook -> строка в замороженной схеме.

    Пустые/односторонние стороны -> NULL, а не выдуманные значения.
    Нет серверной метки -> ts_server_ms/book_age_ms = NULL.
    """
    best_bid_level = _best_level(pb.levels_bids, prefer_highest=True)
    best_ask_level = _best_level(pb.levels_asks, prefer_highest=False)
    best_bid = best_bid_level[0] if best_bid_level else None
    best_ask = best_ask_level[0] if best_ask_level else None
    vwap_bid = (
        vwap_first_qty(pb.levels_bids, best_first=True)
        if pb.levels_bids
        else None
    )
    vwap_ask = (
        vwap_first_qty(pb.levels_asks, best_first=False)
        if pb.levels_asks
        else None
    )
    spread = (
        round(best_ask - best_bid, 8)
        if best_bid is not None and best_ask is not None
        else None
    )
    book_age_ms = (
        ts_recv_ms - pb.server_ts_ms if pb.server_ts_ms is not None else None
    )
    return {
        "ts_recv_ms": ts_recv_ms,
        "ts_server_ms": pb.server_ts_ms,
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": best_bid_level[1] if best_bid_level else None,
        "ask_size": best_ask_level[1] if best_ask_level else None,
        "spread": spread,
        "vwap_bid_100": vwap_bid,
        "vwap_ask_100": vwap_ask,
        "book_age_ms": book_age_ms,
        "seq": seq,
    }


class RateGuard:
    """Ограничитель: не более max_per_window запросов за window_s секунд."""

    def __init__(self, max_per_window: int, window_s: float) -> None:
        self.max_per_window = max_per_window
        self.window_s = window_s
        self._stamps: deque[float] = deque()

    def wait_if_needed(self) -> None:
        """Спит, если окно исчерпано; затем регистрирует запрос."""
        while True:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= self.window_s:
                self._stamps.popleft()
            if len(self._stamps) < self.max_per_window:
                self._stamps.append(time.monotonic())
                return
            time.sleep(0.25)


def poll_token(
    client: httpx.Client,
    token_id: str,
    *,
    seq: int,
    retries: int = RETRY_ATTEMPTS,
) -> dict[str, Any] | None:
    """Один запрос /book -> строка. None при невосстановимом отказе.

    ts_recv_ms снимается в момент получения ответа. retry на 429/5xx/сеть.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            t0 = time.time()
            r = client.get(
                BOOK_URL,
                params={"token_id": token_id},
                headers={"User-Agent": USER_AGENT},
            )
            ts_recv_ms = int(time.time() * 1000)
        except httpx.HTTPError as exc:
            last_err = exc
            time.sleep(min(0.5 * (2**attempt), 4.0))
            continue
        if r.status_code in _RETRY_STATUS and attempt < retries - 1:
            last_err = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(min(0.5 * (2**attempt), 4.0))
            continue
        if r.status_code >= 400:
            return None
        try:
            payload = r.json()
        except ValueError:
            return None
        try:
            pb = parse_book(payload)
        except ValueError:
            return None
        return build_row(
            pb, token_id=token_id, ts_recv_ms=ts_recv_ms, seq=seq
        )
    return None


def run_poll(
    sets: Sequence[tuple[Sequence[str], Path]],
    *,
    duration_s: float = 600.0,
    interval_s: float = POLL_INTERVAL_S,
    max_requests_per_10s: int = MAX_REQUESTS_PER_10S,
    retries: int = RETRY_ATTEMPTS,
    max_cycles: int | None = None,
    client: httpx.Client | None = None,
) -> list[PollSummary]:
    """Циклический опрос НАБОРОВ токенов и запись parquet в замороженной схеме.

    Каждый элемент `sets` -- пара (tokens, out_path): свой набор токенов пишется
    в свой файл. Все наборы опрашиваются В ОДНОМ цикле (общий интервал), чтобы
    параллельные наборы не удваивали время прогона.

    seq -- счётчик цикла: каждый полный проход по наборам инкрементируется на 1,
    все строки цикла несут одно значение seq. Стартует с 1.

    Returns:
        По одному PollSummary на набор (в порядке sets). Файлы создаются в конце
        прогона (не раньше реальных данных).
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=15.0)
    guard = RateGuard(max_requests_per_10s, 10.0)

    acc_rows: list[list[dict[str, Any]]] = [[] for _ in sets]
    n_empty = [0] * len(sets)
    n_incomplete = [0] * len(sets)
    n_failed = [0] * len(sets)
    n_with_server_ts = [0] * len(sets)
    n_requests = [0] * len(sets)

    seq = 0
    cycles = 0
    t_start = time.monotonic()
    try:
        while True:
            if max_cycles is not None and cycles >= max_cycles:
                break
            elapsed = time.monotonic() - t_start
            if elapsed >= duration_s:
                break
            cycle_start = time.monotonic()
            seq += 1
            cycles += 1
            for si, (tokens, _path) in enumerate(sets):
                for token_id in tokens:
                    guard.wait_if_needed()
                    row = poll_token(client, token_id, seq=seq, retries=retries)
                    n_requests[si] += 1
                    if row is None:
                        n_failed[si] += 1
                        continue
                    pb_empty = (
                        row["best_bid"] is None and row["best_ask"] is None
                    )
                    pb_incomplete = (row["best_bid"] is None) != (
                        row["best_ask"] is None
                    )
                    if pb_empty:
                        n_empty[si] += 1
                    if pb_incomplete:
                        n_incomplete[si] += 1
                    if row["ts_server_ms"] is not None:
                        n_with_server_ts[si] += 1
                    acc_rows[si].append(row)
            wait = interval_s - (time.monotonic() - cycle_start)
            if wait > 0:
                time.sleep(wait)
    finally:
        if own_client:
            client.close()

    elapsed = time.monotonic() - t_start
    summaries: list[PollSummary] = []
    for si, (_tokens, out_path) in enumerate(sets):
        out = None
        if acc_rows[si]:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pylist(acc_rows[si], schema=BOOK_SCHEMA)
            pq.write_table(table, out_path)
            out = out_path
        summaries.append(
            PollSummary(
                rows_written=len(acc_rows[si]),
                unique_tokens=len({r["token_id"] for r in acc_rows[si]}),
                n_empty=n_empty[si],
                n_incomplete=n_incomplete[si],
                n_failed=n_failed[si],
                n_with_server_ts=n_with_server_ts[si],
                cycles=cycles,
                requests=n_requests[si],
                elapsed_s=elapsed,
                out_path=out,
            )
        )
    return summaries


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _print_summary(tag: str, s: PollSummary) -> None:
    print(f"[{tag}] строк: {s.rows_written}; уникальных токенов: {s.unique_tokens}")
    print(
        f"[{tag}] циклов: {s.cycles}; запросов: {s.requests}; "
        f"запросов/с: {s.requests_per_second:.3f}"
    )
    print(
        f"[{tag}] пустых книг: {s.n_empty}; неполных: {s.n_incomplete}; "
        f"отказов: {s.n_failed}"
    )
    print(
        f"[{tag}] ответов с серверной меткой: {s.n_with_server_ts} из "
        f"{s.rows_written}"
    )
    print(f"[{tag}] файл: {s.out_path}")


def main(argv: Sequence[str] | None = None) -> int:
    """Прогон: 15-минутные рынки (основной набор) + 2 пятиминутных (отдельно)."""
    from .discovery import updown_outcomes

    duration_s = float(
        (argv[1] if argv and len(argv) > 1 else "600") or 600
    )
    out_dir = Path("data") / "validate"

    with httpx.Client(base_url="https://gamma-api.polymarket.com", timeout=30.0) as gc:
        res = updown_outcomes(gc)
    outcomes = res.outcomes
    tokens_15m: list[str] = []
    tokens_5m: list[str] = []
    five_markets: list[str] = []
    for o in outcomes:
        if "-15m-" in o.market_slug:
            tokens_15m.append(o.token_id)
        elif "-5m-" in o.market_slug and len(five_markets) < 2:
            if o.market_slug not in five_markets:
                five_markets.append(o.market_slug)
            tokens_5m.append(o.token_id)

    stamp = utc_stamp()
    main_path = out_dir / f"book_poll_15m_{stamp}.parquet"
    extra_path = out_dir / f"book_poll_5m_{stamp}.parquet"

    print(f"Основной набор (15m): {len(tokens_15m)} токенов")
    print(f"Дополнительный набор (2x5m): {len(tokens_5m)} токенов")
    if not tokens_15m:
        print("15m токенов не найдено, прогон отменён")
        return 2

    sets = [(tokens_15m, main_path)]
    if tokens_5m:
        sets.append((tokens_5m, extra_path))
    s_main, *rest = run_poll(sets, duration_s=duration_s)
    _print_summary("15m", s_main)
    if rest:
        _print_summary("5m", rest[0])
    else:
        print("[5m] пятиминутных токенов в discovery не было, отдельный файл не создан")
    print("Серверная метка времени в /book: ЕСТЬ, 13-значное целое, epoch ms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
