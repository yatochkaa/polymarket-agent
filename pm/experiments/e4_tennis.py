"""Э4. Теннис: объём, частота разрешений, доля споров UMA за 90 дней.

Назначение: это гейт осуществимости цели B и основа цели C.
Если теннисный сегмент слишком тонкий, исследование B завершается как
UNDECIDABLE ПО МОЩНОСТИ ещё до сбора поведенческих данных -- и это дёшевый
вывод, который честно сделать первым.

Ключевой расчёт мощности: число РАЗРЕШЁННЫХ теннисных событий за 90 дней
задаёт верхнюю границу числа кластеров. Если событий мало, то любой
отдельный трейдер имеет ещё меньше, и кластерные SE будут такими
широкими, что критерий 2.5*sqrt(...) недостижим при реалистичном edge.

Статус знания:
- (в) Способ узнать факт спора UMA через поля Gamma (umaResolutionStatus,
  hasReviewedDates, disputed) -- предположение. Модуль собирает все
  кандидатные поля и выводит их заполненность, чтобы видеть, измерена
  ли величина вообще. Доля споров, вычисленная по полю, которое
  заполнено у 3% рынков, НЕ является долей споров.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..httpc import ReadClient
from ..markets import Market, iter_markets

log = logging.getLogger(__name__)

# (в) Кандидатные поля, в которых может быть признак спора.
_DISPUTE_FIELDS: tuple[str, ...] = (
    "umaResolutionStatus",
    "umaResolutionStatuses",
    "disputed",
    "hasDispute",
    "umaDisputed",
    "resolutionSource",
)
_TENNIS_TAGS: tuple[str, ...] = ("tennis", "atp", "wta", "grand-slam")


@dataclass(slots=True)
class E4Report:
    """Сводка по теннисному сегменту."""

    window_days: int
    n_markets: int
    n_resolved: int
    resolutions_per_week: float | None
    volume_24h_total: float | None
    volume_24h_median: float | None
    volume_24h_p90: float | None
    share_below_1k: float | None
    dispute_field_coverage: dict[str, float]
    dispute_share: float | None
    dispute_share_is_measurable: bool
    power_note: str
    max_clusters_available: int
    notes: list[str] = field(default_factory=list)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(round(q * len(s))) - 1))
    return s[idx]


def is_tennis(m: Market) -> bool:
    """Грубая, но прозрачная классификация теннисного рынка."""
    hay = " ".join([*m.tags, m.slug or "", m.question or ""]).lower()
    return any(t in hay for t in _TENNIS_TAGS)


def power_note(n_markets: int) -> str:
    """Формулирует факт по числу разрешённых рынков в окне.

    Args:
        n_markets: число разрешённых теннисных рынков в окне.

    Returns:
        Строка с фактическим описанием доступного объёма данных.
    """
    return (
        f"Всего {n_markets} разрешённых рынков в окне. Потолок кластеров = "
        f"{n_markets}. Гейт G4 (>= 100 матчей на трейдера) на этапе Э4 не "
        "проверяется: нет данных по адресам. Проверяется на этапе 4 в фильтре 1."
    )


def run(
    settings: Settings,
    gamma: ReadClient,
    window_days: int = 90,
    low_volume_threshold: float = 1000.0,
) -> E4Report:
    """Собирает профиль теннисного сегмента через Gamma (только чтение).

    Args:
        settings: настройки.
        gamma: клиент Gamma API.
        window_days: размер окна в днях.
        low_volume_threshold: граница "тонкого" рынка, USD/сутки.

    Returns:
        E4Report.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    markets: list[Market] = []
    for tag in _TENNIS_TAGS:
        for m in iter_markets(gamma, tag=tag, max_pages=10):
            markets.append(m)
    # Сервер может игнорировать tag_slug -- фильтруем локально и дедуплицируем.
    uniq: dict[str, Market] = {}
    for m in markets:
        if is_tennis(m):
            uniq[m.condition_id or (m.slug or repr(m.raw)[:64])] = m
    tennis = list(uniq.values())
    notes: list[str] = []
    if not tennis:
        notes.append(
            "Gamma не вернул теннисных рынков ни по одному тегу. Сначала "
            "проверьте имя параметра тега, прежде чем делать вывод о объёме."
        )

    def resolved_in_window(m: Market) -> bool:
        raw = m.raw
        for key in ("endDate", "resolvedAt", "closedTime", "end_date_iso"):
            v = raw.get(key)
            if isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return bool(m.closed) and dt >= cutoff
        return False

    resolved = [m for m in tennis if resolved_in_window(m)]
    vols = [m.volume_24h for m in tennis if m.volume_24h is not None]

    coverage: dict[str, float] = {}
    for f in _DISPUTE_FIELDS:
        present = sum(1 for m in tennis if m.raw.get(f) not in (None, "", []))
        coverage[f] = (present / len(tennis)) if tennis else 0.0

    best_field = max(coverage, key=lambda k: coverage[k]) if coverage else ""
    measurable = bool(tennis) and coverage.get(best_field, 0.0) >= 0.5
    dispute_share: float | None = None
    if measurable and resolved:
        disputed = 0
        for m in resolved:
            v = m.raw.get(best_field)
            if isinstance(v, bool):
                disputed += int(v)
            elif isinstance(v, str):
                disputed += int("disput" in v.lower())
        dispute_share = disputed / len(resolved)
    else:
        notes.append(
            "Доля споров UMA НЕ ИЗМЕРЕНА: ни одно поле признака спора не "
            "заполнено у большинства рынков. Нужен второй источник: логи "
            "оракула UMA по адресу адаптера в Polygon. Не подставлять 0."
        )

    weeks = window_days / 7
    return E4Report(
        window_days=window_days,
        n_markets=len(tennis),
        n_resolved=len(resolved),
        resolutions_per_week=(len(resolved) / weeks) if weeks else None,
        volume_24h_total=sum(vols) if vols else None,
        volume_24h_median=_median(vols),
        volume_24h_p90=_quantile(vols, 0.9),
        share_below_1k=(
            sum(1 for v in vols if v < low_volume_threshold) / len(vols)
            if vols
            else None
        ),
        dispute_field_coverage=coverage,
        dispute_share=dispute_share,
        dispute_share_is_measurable=measurable,
        power_note=power_note(len(resolved)),
        max_clusters_available=len(resolved),
        notes=notes,
    )


def report_dict(r: E4Report) -> dict[str, Any]:
    """Сериализация отчёта Э4."""
    return asdict(r)
