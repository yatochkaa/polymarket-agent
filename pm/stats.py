"""Статистическое ядро: кластерные SE, эмпирический байесовский шринкаж,
BH-FDR и единственная реализация критерия решения.

Методологические правила проекта закодированы здесь, а не в тетрадках:
- единица наблюдения -- событие (матч), а не сделка;
- ранжирование по постериорному среднему со шринкажем;
- критерий: gross_delta - cost > K * sqrt(SE_edge^2 + SE_cost^2), K = 2.5;
- три исхода: GO / NO-GO / UNDECIDABLE (bracket_width > gross_delta).

Зависимости: только стандартная библиотека -- модуль должен быть
тестируем без сети и без numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Literal, Mapping, Sequence

__all__ = [
    "Outcome",
    "ClusterStat",
    "ShrunkEstimate",
    "Decision",
    "cluster_mean_se",
    "shrink",
    "bh_fdr",
    "decide",
]


class Outcome(str, Enum):
    """Три и только три допустимых исхода исследования."""

    GO = "GO"
    NO_GO = "NO-GO"
    UNDECIDABLE = "UNDECIDABLE"


@dataclass(frozen=True, slots=True)
class ClusterStat:
    """Среднее и кластерная SE по событиям.

    Attributes:
        mean: среднее по кластерам (равновесовое по событиям).
        se: стандартная ошибка с кластеризацией по event_id.
        n_clusters: число событий (не сделок).
        n_obs: число исходных наблюдений (сделок) -- только для отчёта.
    """

    mean: float
    se: float
    n_clusters: int
    n_obs: int


@dataclass(frozen=True, slots=True)
class ShrunkEstimate:
    """Постериорная оценка одного трейдера.

    Attributes:
        key: идентификатор (адрес).
        raw: сырое in-sample среднее.
        posterior_mean: оценка со шринкажем (единственное основание для ранжирования).
        posterior_se: SE постериорного среднего.
        shrinkage_fraction: доля шринкажа в [0, 1]; 1.0 = сигнал полностью съеден шумом.
        n_clusters: число событий.
    """

    key: str
    raw: float
    posterior_mean: float
    posterior_se: float
    shrinkage_fraction: float
    n_clusters: int


@dataclass(frozen=True, slots=True)
class Decision:
    """Результат применения критерия. Сериализуется в отчёт целиком."""

    outcome: Outcome
    gross_delta: float
    cost: float
    net: float
    se_edge: float
    se_cost: float
    threshold: float
    bracket_width: float
    k: float
    reason: str


def cluster_mean_se(
    values: Sequence[float],
    event_ids: Sequence[str],
) -> ClusterStat:
    """Среднее и кластерная SE с кластеризацией по событию.

    Сначала агрегирует наблюдения внутри события (среднее по матчу), затем
    считает дисперсию между событиями. Это эквивалент CR2-лайт для среднего
    и намеренно консервативен: внутриматчевая корреляция не уменьшает SE.

    Args:
        values: значения по наблюдениям (например, PnL сделки в долях цены).
        event_ids: идентификаторы событий, такой же длины.

    Returns:
        ClusterStat. При n_clusters < 2 SE = inf (не nan): такой трейдер гарантированно
        не пройдёт критерий, а не сломает сортировку.

    Raises:
        ValueError: при разной длине входов или пустом вводе.
    """
    if len(values) != len(event_ids):
        raise ValueError("values and event_ids must have equal length")
    if not values:
        raise ValueError("empty input")
    buckets: dict[str, list[float]] = {}
    for v, e in zip(values, event_ids):
        buckets.setdefault(e, []).append(float(v))
    per_event = [sum(xs) / len(xs) for xs in buckets.values()]
    m = len(per_event)
    mean = sum(per_event) / m
    if m < 2:
        return ClusterStat(mean=mean, se=math.inf, n_clusters=m, n_obs=len(values))
    var = sum((x - mean) ** 2 for x in per_event) / (m - 1)
    return ClusterStat(
        mean=mean, se=math.sqrt(var / m), n_clusters=m, n_obs=len(values)
    )


def shrink(stats: Mapping[str, ClusterStat]) -> list[ShrunkEstimate]:
    """Эмпирический байес (нормальная иерархическая модель, DerSimonian-Laird).

    theta_i ~ N(mu, tau^2), raw_i | theta_i ~ N(theta_i, se_i^2).
    posterior_mean_i = w_i * raw_i + (1 - w_i) * mu, w_i = tau^2 / (tau^2 + se_i^2).
    shrinkage_fraction_i = 1 - w_i.

    Зачем: ранжирование по сырому in-sample среднему выбирает самый
    шумный аккаунт, а не самый сильный.

    Args:
        stats: отображение трейдер -> ClusterStat.

    Returns:
        Список ShrunkEstimate, отсортированный по posterior_mean по убыванию.
        Трейдеры с бесконечной SE полностью сжимаются в mu.
    """
    finite = {k: s for k, s in stats.items() if math.isfinite(s.se) and s.se > 0}
    if not finite:
        return [
            ShrunkEstimate(k, s.mean, 0.0, math.inf, 1.0, s.n_clusters)
            for k, s in stats.items()
        ]
    keys = list(finite)
    raws = [finite[k].mean for k in keys]
    ses = [finite[k].se for k in keys]
    w0 = [1.0 / se**2 for se in ses]
    mu_fixed = sum(w * r for w, r in zip(w0, raws)) / sum(w0)
    q = sum(w * (r - mu_fixed) ** 2 for w, r in zip(w0, raws))
    df = len(keys) - 1
    c = sum(w0) - sum(w**2 for w in w0) / sum(w0)
    tau2 = max(0.0, (q - df) / c) if c > 0 and df > 0 else 0.0
    w1 = [1.0 / (tau2 + se**2) for se in ses]
    mu = sum(w * r for w, r in zip(w1, raws)) / sum(w1)

    out: list[ShrunkEstimate] = []
    for k, raw, se in zip(keys, raws, ses):
        w = tau2 / (tau2 + se**2) if (tau2 + se**2) > 0 else 0.0
        post = w * raw + (1 - w) * mu
        post_se = math.sqrt(w * se**2) if w > 0 else 0.0
        out.append(
            ShrunkEstimate(
                key=k,
                raw=raw,
                posterior_mean=post,
                posterior_se=post_se,
                shrinkage_fraction=1.0 - w,
                n_clusters=stats[k].n_clusters,
            )
        )
    for k, s in stats.items():
        if k not in finite:
            out.append(ShrunkEstimate(k, s.mean, mu, math.inf, 1.0, s.n_clusters))
    out.sort(key=lambda e: e.posterior_mean, reverse=True)
    return out


def bh_fdr(pvalues: Mapping[str, float], q: float) -> dict[str, bool]:
    """Процедура Benjamini-Hochberg.

    Args:
        pvalues: трейдер -> p-value двустороннего теста.
        q: целевой FDR.

    Returns:
        трейдер -> отклонена ли нулевая гипотеза.

    Raises:
        ValueError: если q вне (0, 1).
    """
    if not (0 < q < 1):
        raise ValueError("q must be in (0,1)")
    if not pvalues:
        return {}
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    n = len(items)
    k_max = 0
    for i, (_, p) in enumerate(items, start=1):
        if p <= q * i / n:
            k_max = i
    rejected = {k: False for k in pvalues}
    for k, _ in items[:k_max]:
        rejected[k] = True
    return rejected


def decide(
    gross_delta: float,
    cost: float,
    se_edge: float,
    se_cost: float,
    bracket_width: float,
    k: float = 2.5,
) -> Decision:
    """Применяет предрегистрированный критерий. Единственная точка решения.

    Порядок проверок зафиксирован и не подлежит перестановке:
    1. Если bracket_width > gross_delta -> UNDECIDABLE. Неопределённость измерения
       комиссии/цены перекрывает сам эффект -- вывод сделать нельзя.
    2. Иначе GO только при net > k * sqrt(se_edge^2 + se_cost^2) (сумма квадратов,
       НЕ max).
    3. Иначе NO-GO.

    Args:
        gross_delta: валовое преимущество (в тех же единицах, что cost).
        cost: полные издержки (комиссия из fees.py + спред/слиппаж).
        se_edge: SE оценки edge (кластерная, по событиям).
        se_cost: SE оценки издержек.
        bracket_width: ширина интервала неразрешённой неопределённости (Э1+Э2).
        k: множитель критерия, по умолчанию 2.5.

    Returns:
        Decision со всеми промежуточными числами для отчёта.

    Raises:
        ValueError: при отрицательных SE или bracket_width.
    """
    if se_edge < 0 or se_cost < 0 or bracket_width < 0:
        raise ValueError("se_edge, se_cost, bracket_width must be >= 0")
    net = gross_delta - cost
    threshold = k * math.sqrt(se_edge**2 + se_cost**2)
    if bracket_width > gross_delta:
        return Decision(
            outcome=Outcome.UNDECIDABLE,
            gross_delta=gross_delta,
            cost=cost,
            net=net,
            se_edge=se_edge,
            se_cost=se_cost,
            threshold=threshold,
            bracket_width=bracket_width,
            k=k,
            reason=(
                f"bracket_width={bracket_width:.6f} > gross_delta={gross_delta:.6f}: "
                "неопределённость измерения больше измеряемого эффекта"
            ),
        )
    if net > threshold:
        outcome, reason = Outcome.GO, (
            f"net={net:.6f} > {k}*sqrt(se_edge^2+se_cost^2)={threshold:.6f}"
        )
    else:
        outcome, reason = Outcome.NO_GO, (
            f"net={net:.6f} <= {k}*sqrt(se_edge^2+se_cost^2)={threshold:.6f}"
        )
    return Decision(
        outcome=outcome,
        gross_delta=gross_delta,
        cost=cost,
        net=net,
        se_edge=se_edge,
        se_cost=se_cost,
        threshold=threshold,
        bracket_width=bracket_width,
        k=k,
        reason=reason,
    )
