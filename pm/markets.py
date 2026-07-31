"""Работа со справочником рынков (Gamma API) и классификацией вертикалей.

Статус знания:
- (в) ПРЕДПОЛОЖЕНИЕ: имена полей Gamma (clobTokenIds, volume24hr, bestBid,
  bestAsk, umaResolutionStatus) и их типы. Парсинг толерантен.
- ФАКТ (probe 2026-07-31): Gamma /markets И /markets/keyset молча игнорируют
  tag_slug (отдают нетегированный список с 200 OK). Фильтрация по тегу —
  ТОЛЬКО через /events?tag_slug=. Офсетная пагинация /events упирается в
  потолок offset=2000. Поэтому: тегированные выборки -> iter_events
  (нарезка по датам, без потолка); полное нетегированное перечисление ->
  iter_markets_keyset (курсор). Неуверенная классификация вертикали НЕ падает
  в "politics", а возвращает None.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from .fees import Vertical
from .httpc import ReadClient

log = logging.getLogger(__name__)

_VERTICAL_KEYWORDS: dict[Vertical, tuple[str, ...]] = {
    Vertical.SPORTS: (
        "sports", "tennis", "atp", "wta", "nba", "nfl", "mlb", "nhl",
        "soccer", "football", "ufc", "cricket", "golf", "f1",
    ),
    Vertical.CRYPTO: ("crypto", "bitcoin", "ethereum", "solana", "btc", "eth"),
    Vertical.GEOPOLITICS: (
        "geopolitics", "war", "ceasefire", "nato", "ukraine", "middle east",
    ),
    Vertical.POLITICS: (
        "politics", "election", "senate", "congress", "president", "fed",
        "nomination", "primary",
    ),
}

# ФАКТ (probe 2026-07-31): offset 2000 -> 200 OK, 2001 -> 422. Нарезка по датам
# держит срез ниже потолка; упор = обрезка, а не конец данных.
_OFFSET_CEILING = 2000

# (в) ПРЕДПОЛОЖЕНИЕ: имя параметра запроса для курсора keyset. В ОТВЕТЕ
# поле next_cursor; имя в ЗАПРОСЕ отдельным замером не подтверждён.
# Пагинация защищена детектором непродвижения (см. iter_markets_keyset).
_KEYSET_CURSOR_PARAM = "next_cursor"


@dataclass(slots=True)
class Market:
    condition_id: str | None
    slug: str | None
    question: str | None
    token_ids: list[str]
    volume_24h: float | None
    tags: list[str]
    closed: bool | None
    neg_risk: bool | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_binary(self) -> bool:
        return len(self.token_ids) == 2

    def vertical(self) -> Vertical | None:
        haystack = " ".join([*self.tags, self.slug or "", self.question or ""]).lower()
        for vertical, words in _VERTICAL_KEYWORDS.items():
            if any(w in haystack for w in words):
                return vertical
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _token_ids(raw: dict[str, Any]) -> list[str]:
    v = raw.get("clobTokenIds") or raw.get("clob_token_ids") or raw.get("tokens")
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    if isinstance(v, list):
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                tid = item.get("token_id") or item.get("tokenId")
                if isinstance(tid, str):
                    out.append(tid)
        return out
    return []


def _tags_of(raw: dict[str, Any]) -> list[str]:
    tags_raw = raw.get("tags") or []
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str):
                tags.append(t.lower())
            elif isinstance(t, dict):
                label = t.get("label") or t.get("slug")
                if isinstance(label, str):
                    tags.append(label.lower())
    return tags


def parse_market(raw: dict[str, Any], *, extra_tags: Sequence[str] = ()) -> Market:
    """Сырой JSON Gamma -> Market без потери исходника.

    extra_tags: теги, которых на самом рынке нет (напр. теги родительского
    события из /events, где вложенные рынки поля tags не несут).
    """
    tags = _tags_of(raw)
    for t in extra_tags:
        tl = t.lower()
        if tl not in tags:
            tags.append(tl)
    return Market(
        condition_id=raw.get("conditionId") or raw.get("condition_id"),
        slug=raw.get("slug"),
        question=raw.get("question"),
        token_ids=_token_ids(raw),
        volume_24h=_as_float(raw.get("volume24hr") or raw.get("volume_24hr")),
        tags=tags,
        closed=raw.get("closed") if isinstance(raw.get("closed"), bool) else None,
        neg_risk=raw.get("negRisk") if isinstance(raw.get("negRisk"), bool) else None,
        raw=raw,
    )


def _rows_from_payload(payload: Any, key: str) -> list[Any] | None:
    """Список из ответа Gamma, толерантно к форме (list | {key} | {data})."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        v = payload.get(key)
        if isinstance(v, list):
            return v
        v = payload.get("data")
        if isinstance(v, list):
            return v
    return None


def markets_from_event(event: dict[str, Any]) -> list[Market]:
    """Парсит вложенные рынки события, пробрасывая теги события в каждый."""
    event_tags = _tags_of(event)
    out: list[Market] = []
    mkts = event.get("markets")
    if isinstance(mkts, list):
        for r in mkts:
            if isinstance(r, dict):
                out.append(parse_market(r, extra_tags=event_tags))
    return out


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_events(
    gamma: ReadClient,
    *,
    tag: str,
    start: datetime,
    end: datetime,
    closed: bool | None = None,
    slice_days: int = 3,
    page_limit: int = 100,
) -> Iterator[dict[str, Any]]:
    """Ленивый обход СОБЫТИЙ /events с РЕАЛЬНОЙ фильтрацией по тегу.

    Нарезает [start, end) по slice_days, чтобы срез не упёрся в потолок
    офсета. Клиентски перепроверяет endDate по границам среза. Дедуп по slug.
    Инвариант против обрезки: упор в _OFFSET_CEILING -> RuntimeError.
    """
    if not tag:
        raise ValueError(
            "iter_events требует непустой tag; для нетегированного обхода "
            "используйте iter_markets_keyset"
        )
    seen: set[str] = set()
    cur = start
    step = timedelta(days=slice_days)
    while cur < end:
        nxt = min(cur + step, end)
        lo, hi = _fmt(cur), _fmt(nxt)
        offset = 0
        while True:
            params: dict[str, Any] = {
                "limit": page_limit,
                "offset": offset,
                "tag_slug": tag,
                "end_date_min": lo,
                "end_date_max": hi,
            }
            if closed is not None:
                params["closed"] = str(closed).lower()
            env = gamma.get("/events", params)
            rows = _rows_from_payload(env.payload, "events")
            if rows is None:
                log.warning("Unexpected /events payload shape: %s",
                            type(env.payload).__name__)
                return
            for ev in rows:
                if not isinstance(ev, dict):
                    continue
                end_date = ev.get("endDate")
                if isinstance(end_date, str) and not (lo <= end_date < hi):
                    continue
                slug = ev.get("slug")
                key = slug if isinstance(slug, str) else json.dumps(ev, sort_keys=True)[:64]
                if key in seen:
                    continue
                seen.add(key)
                yield ev
            if len(rows) < page_limit:
                break
            offset += page_limit
            if offset >= _OFFSET_CEILING:
                raise RuntimeError(
                    f"Срез {lo}..{hi} упёрся в потолок офсета {_OFFSET_CEILING}: "
                    "уменьшите slice_days. Иначе число событий занижено."
                )
        cur = nxt


def iter_markets_keyset(
    gamma: ReadClient,
    *,
    closed: bool | None = None,
    page_limit: int = 500,
    max_markets: int | None = None,
) -> Iterator[Market]:
    """Полное НЕтегированное перечисление рынков через /markets/keyset.

    tag_slug сознательно НЕ принимается: keyset его игнорирует (probe
    2026-07-31). Курсор — из поля next_cursor ответа. Детектор непродвижения
    останавливает обход, если первая запись страницы не сменилась (напр. при
    неверном имени параметра курсора), вместо молчаливого зацикливания.
    """
    cursor: str | None = None
    prev_first: str | None = None
    yielded = 0
    while True:
        params: dict[str, Any] = {"limit": page_limit}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if cursor:
            params[_KEYSET_CURSOR_PARAM] = cursor
        env = gamma.get("/markets/keyset", params)
        rows = _rows_from_payload(env.payload, "markets")
        if not rows:
            return
        first_id = None
        r0 = rows[0]
        if isinstance(r0, dict):
            first_id = r0.get("id") or r0.get("conditionId")
        if cursor is not None and first_id is not None and first_id == prev_first:
            log.warning("keyset: страница не сменилась (курсор '%s' не сработал); "
                        "остановка во избежание обрезки/зацикливания",
                        _KEYSET_CURSOR_PARAM)
            return
        prev_first = first_id
        for r in rows:
            if isinstance(r, dict):
                yield parse_market(r)
                yielded += 1
                if max_markets is not None and yielded >= max_markets:
                    return
        nxt = None
        if isinstance(env.payload, dict):
            nxt = env.payload.get("next_cursor")
        if not nxt or not isinstance(nxt, str):
            return
        cursor = nxt


def iter_markets(
    gamma: ReadClient,
    *,
    tag: str | None = None,
    closed: bool | None = None,
    page_limit: int = 500,
    max_pages: int = 40,
) -> Iterator[Market]:
    """СОВМЕСТИМАЯ обёртка. Только для нетегированного случая.

    tag задан -> RuntimeError: /markets и keyset игнорируют tag_slug, поэтому
    тегированный обход давал молча неверный список. Для тега — iter_events.
    Без тега -> iter_markets_keyset с бюджетом page_limit*max_pages.
    """
    if tag is not None:
        raise RuntimeError(
            "iter_markets(tag=...) удалён: Gamma /markets и /markets/keyset молча "
            "игнорируют tag_slug (probe 2026-07-31). Для тегированных выборок "
            "используйте iter_events(gamma, tag=..., start=..., end=...)."
        )
    budget = page_limit * max_pages if max_pages else None
    yield from iter_markets_keyset(gamma, closed=closed, max_markets=budget)
