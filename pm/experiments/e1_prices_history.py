"""Э1 (исправленный, Поправка 2). Что возвращает GET /prices-history: mid книги или цену последней сделки?

СТАРЫЙ МЕТОД УДАЛЁН (фальсификация A8): вердикт по сумме YES+NO -> 1
невалиден (mean_sum=1.0, sigma_sum=0.0 на 1122 и 558 точках): сервер выводит NO=1-YES.

ЭТАЛОНЫ (Поправка 2, заморожено до прогона на выборке):
  mid        = /book?token_id -> (best_bid + best_ask) / 2.
  last_trade = РЕАЛЬНАЯ сделка из /trades по ЭТОМУ токену (поле asset), НЕ из /book.

Почему не /book.last_trade_price: probe 2026-07-31 показал, что это поле скачет
между реальной YES-ценой и комплементом 1-p (Kamala: book=0.918, реальная=0.083).

Пять условий на эталон /trades (все выполнены):
  1. Фильтр СТРОГО по asset == token_id (не по market/conditionId: туда попадёт NO -> 1-p).
  2. Поля outcome / outcomeIndex МУСОР (встречен outcomeIndex=999): отбор только по asset.
  3. Давность: last_trade = последняя сделка с ts <= время снимка и не старше окна 90 с.
     Нет такой -> снимок НЕ входит в замер (no_etalon), это НЕ промах.
  4. Время снимка фиксируется ЛОКАЛЬНО в мс (/book не даёт серверной метки).
  5. Контроль различимости: если число наблюдений с |mid-last|>1/2 тика < n_div(=30),
     результат = inconclusive, А НЕ book_mid.

ПОДТВЕРЖДЁННЫЕ ФОРМЫ (probe 2026-07-31, см. SCHEMAS.md):
/book?token_id=<TID>            -> {bids:[{price,size}], asks:[...], tick_size, last_trade_price}  (всё строки)
/prices-history?market=<TID>&startTs&endTs&fidelity -> {"history":[{"t","p"}]}   ("market" = token id!)
/trades?market=<conditionId>    -> [ {asset, price, size, side, timestamp, ...} ]   (фильтр по asset клиентски)
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Sequence

from ..config import (
    E1_ALIGN_WINDOW_S,
    E1_CROSS_RATE_MAX,
    E1_LIVE_INTERVAL_S,
    E1_LIVE_SAMPLES,
    E1_MATCH_RATE_MIN,
    E1_MATCH_TICK_FRAC,
    E1_MIN_DIVERGENT,
    Settings,
)
from ..httpc import Envelope, HttpFailure, ReadClient

log = logging.getLogger(__name__)

E1Verdict = Literal["book_mid", "last_trade", "inconclusive", "no_data"]

# (подтверждено probe) Первый вариант — рабочий; остальные — страховка.
_PH_PARAM_VARIANTS: tuple[dict[str, str], ...] = (
    {"token": "market", "start": "startTs", "end": "endTs"},
    {"token": "market", "start": "startTime", "end": "endTime"},
    {"token": "token_id", "start": "startTs", "end": "endTs"},
)


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# prices-history (испытуемый ряд)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Series:
    token_id: str
    points: list[tuple[int, float]]
    param_variant: dict[str, str] | None
    raw_status: int | None
    empty: bool


def _extract_points(payload: Any) -> list[tuple[int, float]]:
    rows: Iterable[Any]
    if isinstance(payload, dict):
        rows = payload.get("history") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    out: list[tuple[int, float]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = r.get("t", r.get("timestamp"))
        p = r.get("p", r.get("price"))
        try:
            out.append((int(t), float(p)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def fetch_series(
    clob: ReadClient,
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity: int | None = 1,
    param_variant: dict[str, str] | None = None,
) -> Series:
    variants = (param_variant,) if param_variant else _PH_PARAM_VARIANTS
    last_env: Envelope | None = None
    for v in variants:
        assert v is not None
        params: dict[str, Any] = {v["token"]: token_id, v["start"]: start_ts, v["end"]: end_ts}
        if fidelity is not None:
            params["fidelity"] = fidelity
        try:
            env = clob.get("/prices-history", params)
        except HttpFailure as exc:
            log.info("prices-history variant %s failed: %s", v, exc)
            continue
        last_env = env
        pts = _extract_points(env.payload)
        if pts:
            return Series(token_id, pts, v, env.status, empty=False)
    return Series(token_id, [], param_variant, last_env.status if last_env else None, empty=True)


# --------------------------------------------------------------------------
# /book (эталон mid). Условие 4: локальная метка времени в мс.
# --------------------------------------------------------------------------
@dataclass(slots=True)
class BookSample:
    capture_ms: int            # ЛОКАЛЬНОЕ время ответа (unix ms) — авторитетно
    capture_ts: int            # то же в секундах (для привязки к ts сделок/PH)
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    tick: float | None
    book_last_trade_price: float | None  # ТОЛЬКО для справки; В ВЕРДИКТЕ НЕ УЧАСТВУЕТ


def _best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid_prices = [_f(x.get("price")) for x in bids if isinstance(x, dict)]
    ask_prices = [_f(x.get("price")) for x in asks if isinstance(x, dict)]
    bid_prices = [p for p in bid_prices if p is not None]
    ask_prices = [p for p in ask_prices if p is not None]
    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask


def fetch_book_sample(clob: ReadClient, token_id: str) -> BookSample:
    """Один /book?token_id -> BookSample. capture_ms — локальное время в мс."""
    now_ms = int(time.time() * 1000)
    env = clob.get("/book", {"token_id": token_id})
    book = env.payload if isinstance(env.payload, dict) else {}
    best_bid, best_ask = _best_bid_ask(book)
    mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
    return BookSample(
        capture_ms=now_ms,
        capture_ts=now_ms // 1000,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        tick=_f(book.get("tick_size")),
        book_last_trade_price=_f(book.get("last_trade_price")),
    )


def capture_books(
    clob: ReadClient,
    token_ids: Sequence[str],
    n_samples: int = E1_LIVE_SAMPLES,
    interval_s: float = E1_LIVE_INTERVAL_S,
) -> dict[str, list[BookSample]]:
    """Живой захват книг сразу по нескольким токенам в одном цикле.

    n_samples раундов с шагом interval_s; общее время ~ n_samples*interval_s (не на рынок).
    """
    out: dict[str, list[BookSample]] = {t: [] for t in token_ids}
    for i in range(n_samples):
        for t in token_ids:
            try:
                out[t].append(fetch_book_sample(clob, t))
            except HttpFailure as exc:
                log.warning("book sample round %d token %s failed: %s", i, t, exc)
        if i < n_samples - 1:
            time.sleep(interval_s)
    return out


# --------------------------------------------------------------------------
# /trades (ЭТАЛОН last_trade). Условия 1 и 2 — в select_token_trades.
# --------------------------------------------------------------------------
def fetch_trades_raw(data: ReadClient, condition_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Сырые сделки по рынку (market=conditionId — единственный рабочий фильтр).

    Содержит сделки ОБОИХ ног (YES и NO) — разделяем по asset в select_token_trades.
    Пагинация /trades НЕ прощупана -> одна страница (limit).
    """
    try:
        env = data.get("/trades", {"market": condition_id, "limit": limit})
    except HttpFailure as exc:
        log.info("trades fetch failed: %s", exc)
        return []
    return env.payload if isinstance(env.payload, list) else []


def select_token_trades(rows: Sequence[Any], token_id: str) -> list[tuple[int, float]]:
    """Условия 1+2: отбор СТРОГО по asset == token_id; outcome/outcomeIndex игнорируются.

    Возвращает отсортированный по времени список (ts_sec, price).
    """
    out: list[tuple[int, float]] = []
    tid = str(token_id)
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("asset")) != tid:
            continue
        ts = r.get("timestamp")
        p = r.get("price")
        try:
            out.append((int(ts), float(p)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def _last_trade_at(trades: Sequence[tuple[int, float]], t: int, window_s: int) -> tuple[int, float] | None:
    """Условие 3: последняя сделка с ts <= t и (t - ts) <= window_s. Иначе None."""
    best: tuple[int, float] | None = None
    for ts, p in trades:  # отсортировано по возрастанию
        if ts > t:
            break
        if (t - ts) <= window_s:
            best = (ts, p)
    return best


def _nearest_ph(ph_points: Sequence[tuple[int, float]], t: int, window_s: int) -> tuple[int, float] | None:
    best: tuple[int, float] | None = None
    best_dt: int | None = None
    for pt in ph_points:
        dt = abs(pt[0] - t)
        if best_dt is None or dt < best_dt:
            best_dt, best = dt, pt
    if best is not None and best_dt is not None and best_dt <= window_s:
        return best
    return None


# --------------------------------------------------------------------------
# Чистые диагностики (юнит-тестируются)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Counts:
    n_samples: int = 0
    n_warmup: int = 0
    n_ph_points: int = 0
    n_no_etalon: int = 0
    n_no_ph: int = 0
    n_aligned: int = 0
    n_divergent: int = 0
    n_hit_mid: int = 0
    n_hit_last_trade: int = 0
    n_ambiguous: int = 0

    def add(self, other: "Counts") -> None:
        self.n_samples += other.n_samples
        self.n_warmup += other.n_warmup
        self.n_ph_points += other.n_ph_points
        self.n_no_etalon += other.n_no_etalon
        self.n_no_ph += other.n_no_ph
        self.n_aligned += other.n_aligned
        self.n_divergent += other.n_divergent
        self.n_hit_mid += other.n_hit_mid
        self.n_hit_last_trade += other.n_hit_last_trade
        self.n_ambiguous += other.n_ambiguous


def count_stream(
    samples: Sequence[BookSample],
    ph_points: Sequence[tuple[int, float]],
    trades: Sequence[tuple[int, float]],
    *,
    align_window_s: int = E1_ALIGN_WINDOW_S,
    tick_frac: float = E1_MATCH_TICK_FRAC,
    default_tick: float = 0.01,
    market_start_ts: int | None = None,
    warmup_s: int = 60,
) -> Counts:
    """Счёт наблюдений по ОДНОМУ токену. Якорь — снимок книги (mid).

    Для каждого снимка: last_trade = реальная сделка <= время снимка в окне (усл.3);
    если нет -> no_etalon (не промах). PH привязывается к снимку.
    Расходящийся = |mid - last_trade| > tick_frac*tick (усл.5).
    """
    ticks = [s.tick for s in samples if s.tick]
    median_tick = statistics.median(ticks) if ticks else None
    c = Counts(n_samples=len(samples), n_ph_points=len(ph_points))
    for s in samples:
        if market_start_ts is not None and (s.capture_ts - market_start_ts) < warmup_s:
            c.n_warmup += 1
            continue
        if s.mid is None:
            continue
        lt = _last_trade_at(trades, s.capture_ts, align_window_s)
        if lt is None:
            c.n_no_etalon += 1
            continue
        ph = _nearest_ph(ph_points, s.capture_ts, align_window_s)
        if ph is None:
            c.n_no_ph += 1
            continue
        c.n_aligned += 1
        tick = s.tick or median_tick or default_tick
        tol = tick_frac * tick
        if abs(s.mid - lt[1]) <= tol:
            continue  # нет расхождения -> не различает
        c.n_divergent += 1
        hit_mid = abs(ph[1] - s.mid) <= tol
        hit_lt = abs(ph[1] - lt[1]) <= tol
        if hit_mid and not hit_lt:
            c.n_hit_mid += 1
        elif hit_lt and not hit_mid:
            c.n_hit_last_trade += 1
        else:
            c.n_ambiguous += 1
    return c


def decide(
    c: Counts,
    *,
    min_divergent: int = E1_MIN_DIVERGENT,
    match_rate_min: float = E1_MATCH_RATE_MIN,
    cross_rate_max: float = E1_CROSS_RATE_MAX,
) -> tuple[E1Verdict, float | None, float | None, list[str]]:
    """Замороженное решающее правило (Поправка 2). Условие 5 — первым."""
    notes: list[str] = []
    if c.n_aligned == 0:
        return "no_data", None, None, ["Нет выровненных наблюдений (нет снимков/сделок в окне/точек PH)."]
    nd = c.n_divergent
    if nd < min_divergent:
        return (
            "inconclusive",
            (c.n_hit_mid / nd) if nd else None,
            (c.n_hit_last_trade / nd) if nd else None,
            [
                f"Расхождений mid и last (>1/2 тика) {nd} < {min_divergent}: различимость недостаточна "
                "-> inconclusive (не book_mid). Нужны тонкие рынки с широким спредом или длиннее окно."
            ],
        )
    mm = c.n_hit_mid / nd
    ml = c.n_hit_last_trade / nd
    if mm >= match_rate_min and ml <= cross_rate_max:
        return "book_mid", mm, ml, notes
    if ml >= match_rate_min and mm <= cross_rate_max:
        return "last_trade", mm, ml, notes
    notes.append(
        f"Ни одна гипотеза не прошла порог: match_mid={mm:.3f}, match_last_trade={ml:.3f}, ambiguous={c.n_ambiguous}."
    )
    return "inconclusive", mm, ml, notes


@dataclass(slots=True)
class LiveDiagnostics:
    verdict: E1Verdict
    counts: dict[str, int]
    match_mid_rate: float | None
    match_last_trade_rate: float | None
    median_tick: float | None
    notes: list[str] = field(default_factory=list)


def analyze_live(
    samples: Sequence[BookSample],
    ph_points: Sequence[tuple[int, float]],
    trades: Sequence[tuple[int, float]],
    *,
    min_divergent: int = E1_MIN_DIVERGENT,
    align_window_s: int = E1_ALIGN_WINDOW_S,
    tick_frac: float = E1_MATCH_TICK_FRAC,
    match_rate_min: float = E1_MATCH_RATE_MIN,
    cross_rate_max: float = E1_CROSS_RATE_MAX,
    default_tick: float = 0.01,
) -> LiveDiagnostics:
    """Удобная обёртка count_stream + decide для одного токена (тесты/��тладка)."""
    ticks = [s.tick for s in samples if s.tick]
    median_tick = statistics.median(ticks) if ticks else None
    c = count_stream(
        samples, ph_points, trades,
        align_window_s=align_window_s, tick_frac=tick_frac, default_tick=default_tick,
    )
    verdict, mm, ml, notes = decide(
        c, min_divergent=min_divergent, match_rate_min=match_rate_min, cross_rate_max=cross_rate_max,
    )
    return LiveDiagnostics(
        verdict=verdict,
        counts=asdict(c),
        match_mid_rate=mm,
        match_last_trade_rate=ml,
        median_tick=median_tick,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Сборка: выборка рынков -> агрегатный вердикт G1
# --------------------------------------------------------------------------
@dataclass(slots=True)
class MarketRef:
    token_id: str
    condition_id: str
    slug: str | None = None


@dataclass(slots=True)
class E1Report:
    verdict: E1Verdict
    n_markets: int
    capture_start_ts: int | None
    capture_end_ts: int | None
    totals: dict[str, int]
    agg_match_mid_rate: float | None
    agg_match_last_trade_rate: float | None
    per_market: list[dict[str, Any]]
    thresholds: dict[str, float]
    preregistration_commit: str | None
    notes: list[str] = field(default_factory=list)


def run(
    settings: Settings,
    clob: ReadClient,
    data: ReadClient,
    markets: Sequence[MarketRef],
    *,
    n_samples: int = E1_LIVE_SAMPLES,
    interval_s: float = E1_LIVE_INTERVAL_S,
    fidelity: int | None = 1,
    align_window_s: int = E1_ALIGN_WINDOW_S,
    tick_frac: float = E1_MATCH_TICK_FRAC,
    min_divergent: int = E1_MIN_DIVERGENT,
    match_rate_min: float = E1_MATCH_RATE_MIN,
    cross_rate_max: float = E1_CROSS_RATE_MAX,
    preregistration_commit: str | None = None,
) -> E1Report:
    """Корректный Э1 на ВЫБОРКЕ рынков: prices-history vs (mid из /book, last_trade из /trades).

    Живой захват всех рынков в одном цикле; вердикт G1 — по АГРЕГАТНым счётам.
    """
    if not markets:
        raise ValueError("пустая выборка markets — нечего замерять")
    token_ids = [m.token_id for m in markets]
    captured = capture_books(clob, token_ids, n_samples=n_samples, interval_s=interval_s)

    all_ms: list[int] = [s.capture_ms for ss in captured.values() for s in ss]
    cap_start = (min(all_ms) // 1000) if all_ms else None
    cap_end = (max(all_ms) // 1000) if all_ms else None

    totals = Counts()
    per_market: list[dict[str, Any]] = []
    for m in markets:
        samples = captured.get(m.token_id, [])
        cap_ts = [s.capture_ts for s in samples]
        if cap_ts:
            series = fetch_series(clob, m.token_id, min(cap_ts) - 60, max(cap_ts) + 60, fidelity)
            ph_points = [pt for pt in series.points if min(cap_ts) - 60 <= pt[0] <= max(cap_ts) + 60]
        else:
            ph_points = []
        raw = fetch_trades_raw(data, m.condition_id)
        trades = select_token_trades(raw, m.token_id)
        c = count_stream(
            samples, ph_points, trades,
            align_window_s=align_window_s, tick_frac=tick_frac,
        )
        totals.add(c)
        nd = c.n_divergent
        per_market.append(
            {
                "slug": m.slug,
                "token_id": m.token_id,
                "condition_id": m.condition_id,
                "counts": asdict(c),
                "trades_token": len(trades),
                "match_mid_rate": (c.n_hit_mid / nd) if nd else None,
                "match_last_trade_rate": (c.n_hit_last_trade / nd) if nd else None,
            }
        )

    verdict, mm, ml, notes = decide(
        totals, min_divergent=min_divergent, match_rate_min=match_rate_min, cross_rate_max=cross_rate_max,
    )
    return E1Report(
        verdict=verdict,
        n_markets=len(markets),
        capture_start_ts=cap_start,
        capture_end_ts=cap_end,
        totals=asdict(totals),
        agg_match_mid_rate=mm,
        agg_match_last_trade_rate=ml,
        per_market=per_market,
        thresholds={
            "match_rate_min": match_rate_min,
            "cross_rate_max": cross_rate_max,
            "min_divergent": float(min_divergent),
            "tick_frac": tick_frac,
            "align_window_s": float(align_window_s),
        },
        preregistration_commit=preregistration_commit,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Качение (Поправка 2): параллельные цепочки коротких up/down по активам.
# ФИКСИРУЕТСЯ ЗАРАНЕЕ объём захвата (n_cycles x число слотов), НЕ цель по n_div.
# Разогрев warmup_s исключается из знаменателя (см. count_stream).
# n_div < min_divergent -> inconclusive: честный замороженный исход, не повод катиться.
# Открытие рынков (gamma) инъектируется слот-резолверами извне (run_e1); метод к нему агностичен.
# --------------------------------------------------------------------------
@dataclass(slots=True)
class CurrentMarket:
    token_id: str
    condition_id: str
    slug: str | None
    start_ts: int  # эпоха НАЧАЛА окна рынка (для warmup)


class SlotResolver:
    """Слот-цепочка одного актива. key — метка (btc/eth/sol); current() —
    текущий живой рынок цепочки на момент вызова или None (перекат
    происходит сам: каждый цикл заново выбирается живой рынок)."""

    key: str

    def current(self) -> "CurrentMarket | None":
        raise NotImplementedError


@dataclass(slots=True)
class RollingSample:
    slot: str
    token_id: str
    condition_id: str
    slug: str | None
    start_ts: int
    sample: BookSample


def capture_rolling(
    clob: ReadClient,
    resolvers: Sequence[SlotResolver],
    n_cycles: int,
    interval_s: float = E1_LIVE_INTERVAL_S,
) -> list[RollingSample]:
    """РОВНО n_cycles раундов x len(resolvers) слотов. Каждый раунд заново
    определяет текущий рынок каждого слота (это и есть качение) и снимает
    его книгу один раз. Остановка — по объёму, НЕ по n_div."""
    out: list[RollingSample] = []
    for i in range(n_cycles):
        for r in resolvers:
            try:
                cur = r.current()
            except Exception as exc:  # noqa: BLE001 — сбой резолвера не роняет захват
                log.warning("rolling resolve cycle %d slot %s failed: %s", i, getattr(r, "key", "?"), exc)
                cur = None
            if cur is None:
                continue
            try:
                bs = fetch_book_sample(clob, cur.token_id)
            except HttpFailure as exc:
                log.warning("rolling book cycle %d slot %s failed: %s", i, r.key, exc)
                continue
            out.append(RollingSample(r.key, cur.token_id, cur.condition_id, cur.slug, cur.start_ts, bs))
        if i < n_cycles - 1:
            time.sleep(interval_s)
    return out


def run_rolling(
    settings: Settings,
    clob: ReadClient,
    data: ReadClient,
    resolvers: Sequence[SlotResolver],
    *,
    n_cycles: int,
    interval_s: float = E1_LIVE_INTERVAL_S,
    warmup_s: int = 60,
    fidelity: int | None = 1,
    align_window_s: int = E1_ALIGN_WINDOW_S,
    tick_frac: float = E1_MATCH_TICK_FRAC,
    min_divergent: int = E1_MIN_DIVERGENT,
    match_rate_min: float = E1_MATCH_RATE_MIN,
    cross_rate_max: float = E1_CROSS_RATE_MAX,
    preregistration_commit: str | None = None,
) -> E1Report:
    """Э1 на КАЧЕНИИ по цепочкам коротких up/down. Объём захвата фиксирован
    заранее (n_cycles x число слотов)."""
    if not resolvers:
        raise ValueError("нет слотов-цепочек — нечего катить")
    captured = capture_rolling(clob, resolvers, n_cycles, interval_s)

    all_ms = [rs.sample.capture_ms for rs in captured]
    cap_start = (min(all_ms) // 1000) if all_ms else None
    cap_end = (max(all_ms) // 1000) if all_ms else None

    by_market: dict[str, list[RollingSample]] = {}
    for rs in captured:
        by_market.setdefault(rs.token_id, []).append(rs)

    totals = Counts()
    per_market: list[dict[str, Any]] = []
    for token_id, rss in by_market.items():
        samples = [rs.sample for rs in rss]
        start_ts = rss[0].start_ts
        cond = rss[0].condition_id
        cap_ts = [s.capture_ts for s in samples]
        series = fetch_series(clob, token_id, min(cap_ts) - 60, max(cap_ts) + 60, fidelity)
        ph_points = [pt for pt in series.points if min(cap_ts) - 60 <= pt[0] <= max(cap_ts) + 60]
        raw = fetch_trades_raw(data, cond)
        trades = select_token_trades(raw, token_id)
        c = count_stream(
            samples, ph_points, trades,
            align_window_s=align_window_s, tick_frac=tick_frac,
            market_start_ts=start_ts, warmup_s=warmup_s,
        )
        totals.add(c)
        nd = c.n_divergent
        per_market.append(
            {
                "slot": rss[0].slot,
                "slug": rss[0].slug,
                "token_id": token_id,
                "condition_id": cond,
                "counts": asdict(c),
                "trades_token": len(trades),
                "match_mid_rate": (c.n_hit_mid / nd) if nd else None,
                "match_last_trade_rate": (c.n_hit_last_trade / nd) if nd else None,
            }
        )

    verdict, mm, ml, notes = decide(
        totals, min_divergent=min_divergent, match_rate_min=match_rate_min, cross_rate_max=cross_rate_max,
    )
    notes.append(
        "rolling: slots=%d cycles=%d snapshots_plan=%d snapshots_fact=%d markets=%d warmup_s=%d; "
        "obem zafiksirovan zaranee, n_div<min -> inconclusive chestnyy zamorozhennyy iskhod."
        % (len(resolvers), n_cycles, n_cycles * len(resolvers), len(captured), len(by_market), warmup_s)
    )
    return E1Report(
        verdict=verdict,
        n_markets=len(by_market),
        capture_start_ts=cap_start,
        capture_end_ts=cap_end,
        totals=asdict(totals),
        agg_match_mid_rate=mm,
        agg_match_last_trade_rate=ml,
        per_market=per_market,
        thresholds={
            "match_rate_min": match_rate_min,
            "cross_rate_max": cross_rate_max,
            "min_divergent": float(min_divergent),
            "tick_frac": tick_frac,
            "align_window_s": float(align_window_s),
            "warmup_s": float(warmup_s),
            "n_cycles": float(n_cycles),
            "n_slots": float(len(resolvers)),
        },
        preregistration_commit=preregistration_commit,
        notes=notes,
    )
