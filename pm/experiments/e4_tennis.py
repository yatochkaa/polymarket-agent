"""Э4. Теннис: объём, частота разрешений, доля споров UMA за 90 дней.

Назначение: гейт осуществимости цели B и основа цели C.
Единица наблюдения — ОДИНОЧНЫЙ матч (Уточнение 1, 2026-07-31). Парные матчи
(-doubles-) исключены из мощности и идут разведочной веткой вне GO/NO-GO.

Ключевой расчёт мощности: число РАЗРЕШЁННЫХ одиночных теннисных матчей за
90 дней задаёт верхнюю границу числа кластеров.

Источник данных (probe 2026-07-31): Gamma /markets игнорирует tag_slug,
поэтому теннис берём через iter_events(/events?tag_slug=tennis) с нарезкой
по датам. Матч = событие, слаг которого совпадает с маской _MATCH_SLUG.

Статус знания:
- (в) Способ узнать факт спора UMA через поля Gamma — предположение. Модуль
  собирает кандидатные поля и выводит их заполненность. Доля споров по
  полю, заполненному у 3% рынков, НЕ является долей споров.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..httpc import ReadClient
from ..markets import Market, iter_events, markets_from_event

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

# Маска матчевого слага. Единица = одиночный матч (Уточнение 1): парные
# (-doubles-) в основную популяцию не входят.
_MATCH_SLUG = re.compile(r"^(atp|wta)-.*\d{4}-\d{2}-\d{2}$")


def _is_singles_match(slug: str | None) -> bool:
    return bool(slug) and bool(_MATCH_SLUG.match(slug)) and "-doubles-" not in slug


def _is_doubles_match(slug: str | None) -> bool:
    return bool(slug) and bool(_MATCH_SLUG.match(slug)) and "-doubles-" in slug


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
    n_singles_matches: int = 0
    n_doubles_matches: int = 0
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


def power_note(n_matches: int) -> str:
    """Факт по числу разрешённых одиночных матчей в окне."""
    return (
        f"Всего {n_matches} разрешённых одиночных теннисных матчей в окне "
        f"(единица наблюдения). Потолок кластеров = {n_matches}. Гейт G4 "
        "(>= 100 матчей на трейдера) на этапе Э4 не проверяется: нет данных "
        "по адресам. Проверяется на этапе 4 в фильтре 1."
    )


def run(
    settings: Settings,
    gamma: ReadClient,
    window_days: int = 90,
    low_volume_threshold: float = 1000.0,
) -> E4Report:
    """Профиль теннисного сегмента через Gamma (только чтение).

    Единица = одиночный матч (Уточнение 1). Матчи — события /events с тегом
    tennis, слаг которых совпадает с _MATCH_SLUG и не содержит -doubles-.
    Объём и признаки спора считаются на уровне рынков ВНУТРИ этих матчей
    (у события полей volume24hr/umaResolutionStatus нет).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    notes: list[str] = []

    events = list(
        iter_events(gamma, tag="tennis", start=cutoff, end=now, closed=True)
    )

    singles: dict[str, dict[str, Any]] = {}
    doubles: dict[str, dict[str, Any]] = {}
    for ev in events:
        slug = ev.get("slug")
        if _is_singles_match(slug):
            singles[slug] = ev
        elif _is_doubles_match(slug):
            doubles[slug] = ev

    if not singles:
        notes.append(
            "iter_events не вернул одиночных теннисных матчей в окне. Проверьте "
            "окно и маску слага прежде, чем делать вывод о мощности."
        )

    match_markets: list[Market] = []
    for ev in singles.values():
        match_markets.extend(markets_from_event(ev))

    vols = [m.volume_24h for m in match_markets if m.volume_24h is not None]

    coverage: dict[str, float] = {}
    for f in _DISPUTE_FIELDS:
        present = sum(1 for m in match_markets if m.raw.get(f) not in (None, "", []))
        coverage[f] = (present / len(match_markets)) if match_markets else 0.0

    best_field = max(coverage, key=lambda k: coverage[k]) if coverage else ""
    measurable = bool(match_markets) and coverage.get(best_field, 0.0) >= 0.5
    dispute_share: float | None = None
    if measurable and match_markets:
        disputed = 0
        for m in match_markets:
            v = m.raw.get(best_field)
            if isinstance(v, bool):
                disputed += int(v)
            elif isinstance(v, str):
                disputed += int("disput" in v.lower())
        dispute_share = disputed / len(match_markets)
    else:
        notes.append(
            "Доля споров UMA НЕ ИЗМЕРЕНА: ни одно поле признака спора не "
            "заполнено у большинства рынков. Нужен второй источник: логи "
            "оракула UMA по адресу адаптера в Polygon. Не подставлять 0."
        )

    notes.append(
        f"Единица = одиночный матч (Уточнение 1, 2026-07-31). Парные "
        f"({len(doubles)}) исключены из мощности как разведочная ветка."
    )

    n_singles = len(singles)
    weeks = window_days / 7
    return E4Report(
        window_days=window_days,
        n_markets=len(match_markets),
        n_resolved=n_singles,
        resolutions_per_week=(n_singles / weeks) if weeks else None,
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
        power_note=power_note(n_singles),
        max_clusters_available=n_singles,
        n_singles_matches=n_singles,
        n_doubles_matches=len(doubles),
        notes=notes,
    )


def report_dict(r: E4Report) -> dict[str, Any]:
    """Сериализация отчёта Э4."""
    return asdict(r)
