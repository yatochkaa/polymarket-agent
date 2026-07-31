"""Э1. Что возвращает GET /prices-history: mid стакана или цену последней сделки.

Логика теста (зафиксирована в PREREGISTRATION.md до запуска):
для бинарного рынка YES + NO должны давать 1.0.
- Если ряд -- mid стакана, сумма держится около 1 с малым шумом
  (книга когерентна по комплементам в каждый момент).
- Если ряд -- цена последней сделки, две ноги обновляются в разные моменты
  и с разных сторон спреда, появляется систематическое смещение.

Важные уточнения к исходной схеме теста (без них тест невалиден):
1. Сумма считается только по ТОЧНО совпадающим меткам времени. Любой
   forward-fill сам создаёт дрейф и гарантирует ложный вывод "last trade".
   Потому же отчёт отдельно несёт timestamp_alignment: долю совпавших меток.
   Если совпадений почти нет -- это само по себе улика против "сетки mid".
2. Симметричный стакан с шагом 0.01 может давать сумму 1.00 и для last trade,
   если сделки шли в обе ноги ровно. Поэтому добавлен второй, независимый
   диагностический признак: доля точек, попавших ровно в сетку tick
   (grid_hit_rate). Mid половинного спреда часто даёт полутики (x.xx5),
   цена сделки почти всегда лежит ровно на тике.
3. Третий признак: ведёт ли fidelity=1 к точкам в минуты без сделок. Сетка
   без пропусков на неликвидном рынке = ряд строится из состояния книги
   либо просто форвард-филлится сервером -- различается по числу повторов
   одного и того же значения (repeat_run_share).

Статус знания: (в) имена параметров (market/startTs/endTs/fidelity/interval)
и форма ответа {"history": [{"t":..,"p":..}]} -- предположение. Модуль пробует
альтернативные имена и фиксирует, какой вариант сработал.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Sequence

from ..config import E1_DRIFT_TRADE_MIN, E1_SIGMA_BOOK_MAX, Settings
from ..httpc import Envelope, HttpFailure, ReadClient
from ..markets import Market, iter_markets

log = logging.getLogger(__name__)

E1Verdict = Literal["book_mid", "last_trade", "inconclusive", "no_data"]

# (в) Порядок попыток по именам параметров. Первый сработавший фиксируется.
_PARAM_VARIANTS: tuple[dict[str, str], ...] = (
    {"token": "market", "start": "startTs", "end": "endTs"},
    {"token": "market", "start": "startTime", "end": "endTime"},
    {"token": "token_id", "start": "startTs", "end": "endTs"},
)


@dataclass(slots=True)
class Series:
    """Ряд цен одного токена."""

    token_id: str
    points: list[tuple[int, float]]  # (unix_seconds, price)
    param_variant: dict[str, str] | None
    raw_status: int | None
    empty: bool

    def as_map(self) -> dict[int, float]:
        """Словарь ts -> price. При дублях побеждает последнее значение."""
        return {t: p for t, p in self.points}


@dataclass(slots=True)
class PairDiagnostics:
    """Диагностика по одному бинарному рынку."""

    condition_id: str | None
    slug: str | None
    volume_24h: float | None
    n_yes: int
    n_no: int
    n_matched_ts: int
    timestamp_alignment: float
    mean_sum: float | None
    sigma_sum: float | None
    mean_abs_dev: float | None
    max_abs_dev: float | None
    grid_hit_rate: float | None
    repeat_run_share: float | None
    fidelity: int | None
    verdict: E1Verdict
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class E1Report:
    """Сводный результат Э1."""

    verdict: E1Verdict
    n_markets_tested: int
    n_markets_with_data: int
    pooled_sigma_sum: float | None
    pooled_mean_abs_dev: float | None
    coverage_low_volume: dict[str, Any]
    fidelity_probe: dict[str, Any]
    param_variant_used: dict[str, str] | None
    per_market: list[dict[str, Any]]
    thresholds: dict[str, float]


def _extract_points(payload: Any) -> list[tuple[int, float]]:
    """Извлекает (t, p) из возможных форм ответа без догадок о порядке."""
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
    fidelity: int | None = None,
    param_variant: dict[str, str] | None = None,
) -> Series:
    """Загружает /prices-history для одного токена.

    Перебирает варианты имён параметров, если variant не задан.

    Args:
        clob: клиент CLOB.
        token_id: ERC-1155 token id одной ноги.
        start_ts: начало окна, unix секунды.
        end_ts: конец окна, unix секунды.
        fidelity: шаг в минутах (1 = минутный).
        param_variant: зафиксированный набор имён параметров.

    Returns:
        Series. Пустой результат -- не ошибка, а данные (empty=True).
    """
    variants = (param_variant,) if param_variant else _PARAM_VARIANTS
    last_env: Envelope | None = None
    for v in variants:
        assert v is not None
        params = {v["token"]: token_id, v["start"]: start_ts, v["end"]: end_ts}
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
    return Series(
        token_id,
        [],
        param_variant,
        last_env.status if last_env else None,
        empty=True,
    )


def _grid_hit_rate(prices: Sequence[float], tick: float = 0.01) -> float | None:
    """Доля цен, лежащих ровно на сетке tick (с допуском 1e-9)."""
    if not prices:
        return None
    hits = sum(1 for p in prices if abs((p / tick) - round(p / tick)) < 1e-6)
    return hits / len(prices)


def _repeat_run_share(prices: Sequence[float]) -> float | None:
    """Доля точек, равных предыдущей (признак forward-fill)."""
    if len(prices) < 2:
        return None
    same = sum(1 for a, b in zip(prices, prices[1:]) if a == b)
    return same / (len(prices) - 1)


def analyze_pair(
    market: Market,
    yes: Series,
    no: Series,
    fidelity: int | None,
) -> PairDiagnostics:
    """Считает статистики по одному рынку строго по совпавшим меткам времени.

    Returns:
        PairDiagnostics с полевым вердиктом по данному рынку.
    """
    notes: list[str] = []
    my, mn = yes.as_map(), no.as_map()
    shared = sorted(set(my) & set(mn))
    union = len(set(my) | set(mn))
    alignment = (len(shared) / union) if union else 0.0
    sums = [my[t] + mn[t] for t in shared]
    devs = [s - 1.0 for s in sums]
    all_prices = [p for _, p in yes.points] + [p for _, p in no.points]

    mean_sum = statistics.fmean(sums) if sums else None
    sigma = statistics.stdev(sums) if len(sums) > 1 else None
    mean_abs = statistics.fmean([abs(d) for d in devs]) if devs else None
    max_abs = max((abs(d) for d in devs), default=None)

    verdict: E1Verdict
    if not sums:
        verdict = "no_data"
        notes.append(
            "Нет совпадающих меток времени между ногами: само по себе улика "
            "против единой сетки mid, но не доказательство last-trade."
        )
    elif sigma is not None and sigma < E1_SIGMA_BOOK_MAX and (mean_abs or 0) < 0.01:
        verdict = "book_mid"
    elif (mean_abs or 0) >= E1_DRIFT_TRADE_MIN or (
        sigma is not None and sigma >= E1_DRIFT_TRADE_MIN
    ):
        verdict = "last_trade"
    else:
        verdict = "inconclusive"
        notes.append(
            "Отклонение попадает в серую зону между порогами 0.005 и 0.02."
        )
    if len(shared) < 30:
        notes.append(f"Мало совпавших точек ({len(shared)}): вердикт неустойчив.")

    return PairDiagnostics(
        condition_id=market.condition_id,
        slug=market.slug,
        volume_24h=market.volume_24h,
        n_yes=len(yes.points),
        n_no=len(no.points),
        n_matched_ts=len(shared),
        timestamp_alignment=alignment,
        mean_sum=mean_sum,
        sigma_sum=sigma,
        mean_abs_dev=mean_abs,
        max_abs_dev=max_abs,
        grid_hit_rate=_grid_hit_rate(all_prices),
        repeat_run_share=_repeat_run_share([p for _, p in yes.points]),
        fidelity=fidelity,
        verdict=verdict,
        notes=notes,
    )


def run(
    settings: Settings,
    clob: ReadClient,
    gamma: ReadClient,
    start_ts: int,
    end_ts: int,
    n_markets: int = 12,
    fidelity: int | None = 1,
    low_volume_threshold: float = 1000.0,
) -> E1Report:
    """Выполняет Э1 целиком: вердикт по источнику цены, покрытие, fidelity.

    Выборка намеренно стратифицирована: половина ликвидных, половина
    с оборотом < low_volume_threshold. На ликвидном рынке mid и last trade почти
    совпадают, так что тест на одних ликвидных рынках бессилен.

    Args:
        settings: настройки.
        clob: клиент CLOB.
        gamma: клиент Gamma.
        start_ts: начало окна (unix секунды).
        end_ts: конец окна.
        n_markets: сколько рынков взять.
        fidelity: значение fidelity для основного теста.
        low_volume_threshold: граница низкого оборота в USD/сутки.

    Returns:
        E1Report.
    """
    binaries = [
        m
        for m in iter_markets(gamma, closed=False, max_pages=4)
        if m.is_binary
    ]
    liquid = sorted(
        (m for m in binaries if (m.volume_24h or 0) >= low_volume_threshold),
        key=lambda m: -(m.volume_24h or 0),
    )
    illiquid = [m for m in binaries if (m.volume_24h or 0) < low_volume_threshold]
    half = max(1, n_markets // 2)
    sample = liquid[:half] + illiquid[:half]
    if not sample:
        raise RuntimeError(
            "Gamma не вернул ни одного бинарного рынка: проверьте gamma_host и "
            "имена полей в pm/markets.py перед интерпретацией Э1."
        )

    diags: list[PairDiagnostics] = []
    variant_used: dict[str, str] | None = None
    covered_low, total_low = 0, 0
    for m in sample:
        yes = fetch_series(clob, m.token_ids[0], start_ts, end_ts, fidelity, variant_used)
        variant_used = variant_used or yes.param_variant
        no = fetch_series(clob, m.token_ids[1], start_ts, end_ts, fidelity, variant_used)
        d = analyze_pair(m, yes, no, fidelity)
        diags.append(d)
        if (m.volume_24h or 0) < low_volume_threshold:
            total_low += 1
            covered_low += 1 if (yes.points or no.points) else 0

    with_data = [d for d in diags if d.n_matched_ts > 0]
    pooled_sigma = (
        statistics.fmean([d.sigma_sum for d in with_data if d.sigma_sum is not None])
        if any(d.sigma_sum is not None for d in with_data)
        else None
    )
    pooled_abs = (
        statistics.fmean(
            [d.mean_abs_dev for d in with_data if d.mean_abs_dev is not None]
        )
        if any(d.mean_abs_dev is not None for d in with_data)
        else None
    )

    votes = [d.verdict for d in with_data]
    if not votes:
        overall: E1Verdict = "no_data"
    elif votes.count("book_mid") >= max(3, math.ceil(0.7 * len(votes))):
        overall = "book_mid"
    elif votes.count("last_trade") >= max(3, math.ceil(0.7 * len(votes))):
        overall = "last_trade"
    else:
        overall = "inconclusive"

    # fidelity-проба: тот же токен на неликвидном рынке, fidelity=1 вс без fidelity.
    fidelity_probe: dict[str, Any] = {"tested": False}
    if illiquid:
        tok = illiquid[0].token_ids[0]
        s1 = fetch_series(clob, tok, start_ts, end_ts, 1, variant_used)
        s0 = fetch_series(clob, tok, start_ts, end_ts, None, variant_used)
        expected_minutes = max(1, (end_ts - start_ts) // 60)
        fidelity_probe = {
            "tested": True,
            "slug": illiquid[0].slug,
            "volume_24h": illiquid[0].volume_24h,
            "n_points_fidelity_1": len(s1.points),
            "n_points_default": len(s0.points),
            "expected_minutes_in_window": expected_minutes,
            "grid_completeness_fidelity_1": len(s1.points) / expected_minutes,
            "repeat_run_share_fidelity_1": _repeat_run_share(
                [p for _, p in s1.points]
            ),
            "interpretation": (
                "grid_completeness ~1 и высокий repeat_run_share на рынке без сделок "
                "= сервер заполняет пропуски сам; точки НЕ доказывают наличие сделок"
            ),
        }

    return E1Report(
        verdict=overall,
        n_markets_tested=len(sample),
        n_markets_with_data=len(with_data),
        pooled_sigma_sum=pooled_sigma,
        pooled_mean_abs_dev=pooled_abs,
        coverage_low_volume={
            "threshold_usd_24h": low_volume_threshold,
            "n_low_volume_tested": total_low,
            "n_low_volume_with_any_points": covered_low,
            "coverage_share": (covered_low / total_low) if total_low else None,
        },
        fidelity_probe=fidelity_probe,
        param_variant_used=variant_used,
        per_market=[asdict(d) for d in diags],
        thresholds={
            "sigma_book_max": E1_SIGMA_BOOK_MAX,
            "drift_trade_min": E1_DRIFT_TRADE_MIN,
        },
    )
