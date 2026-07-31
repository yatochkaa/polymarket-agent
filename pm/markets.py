"""Работа со справочником рынков (Gamma API) и классификацией вертикалей.

Статус знания:
- (в) ПРЕДПОЛОЖЕНИЕ: имена полей Gamma (`clobTokenIds`, `volume24hr`,
  `bestBid`, `bestAsk`, `umaResolutionStatus`) и их типы. Поэтому парсинг
  толерантен: неизвестные поля сохраняются в raw и попадают в артефакты,
  а отсутствие поля даёт None, а не исключение.
Классификация вертикали важна только тем, что она выбирает feeRate. Поэтому
неуверенная классификация НЕ молча падает в "politics", а возвращает None.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .fees import Vertical
from .httpc import ReadClient

log = logging.getLogger(__name__)

# (в) Ключевые слова для сопоставления тегов Gamma с вертикалями комиссий.
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


@dataclass(slots=True)
class Market:
    """Нормализованное описание рынка.

    Attributes:
        condition_id: conditionId рынка.
        slug: человекочитаемый идентификатор.
        question: текст вопроса.
        token_ids: список token_id (для бинарного рынка ровно два комплемента).
        volume_24h: оборот за 24ч в USD, если Gamma его вернул.
        tags: теги в нижнем регистре.
        closed: закрыт ли рынок.
        neg_risk: признак neg-risk рынка (влияет на адрес Exchange).
        raw: исходный JSON целиком.
    """

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
        """True, если у рынка ровно два комплементарных токена."""
        return len(self.token_ids) == 2

    def vertical(self) -> Vertical | None:
        """Пытается определить вертикаль комиссии.

        Returns:
            Vertical либо None, если уверенного сопоставления нет. None НЕЛЬЗЯ
            трактовать как "комиссия 0" -- это пропуск рынка из анализа.
        """
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
    """Извлекает token_id. Gamma иногда отдаёт список JSON-строкой."""
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


def parse_market(raw: dict[str, Any]) -> Market:
    """Превращает сырой JSON Gamma в Market без потери исходника."""
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


def iter_markets(
    gamma: ReadClient,
    *,
    tag: str | None = None,
    closed: bool | None = None,
    page_limit: int = 500,
    max_pages: int = 40,
) -> Iterator[Market]:
    """Ленивая пагинация по /markets.

    Args:
        gamma: клиент с base_url на gamma_host.
        tag: фильтр по тегу (например "tennis"). (в) имя параметра может
            отличаться; если сервер игнорирует его, фильтруем локально.
        closed: фильтр по статусу.
        page_limit: размер страницы.
        max_pages: жёсткий предел, чтобы не уйти в бесконечный цикл.

    Yields:
        Market.
    """
    offset = 0
    for _ in range(max_pages):
        env = gamma.get(
            "/markets",
            {
                "limit": page_limit,
                "offset": offset,
                "tag_slug": tag,
                "closed": None if closed is None else str(closed).lower(),
            },
        )
        payload = env.payload
        rows: Sequence[Any]
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            rows = payload["data"]
        else:
            log.warning("Unexpected /markets payload shape: %s", type(payload))
            return
        if not rows:
            return
        for r in rows:
            if isinstance(r, dict):
                yield parse_market(r)
        if len(rows) < page_limit:
            return
        offset += len(rows)
