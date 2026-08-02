"""Обнаружение живых рынков crypto up/down через Gamma /events.

Задача 1 проверочного контура: найти живые (незакрытые) рынки crypto up/down
и вернуть слаг рынка и token_id по каждому исходу.

Источник: gamma-api.polymarket.com/events?tag_slug=crypto&closed=false.
Отбор по маске "updown" в слаге рынка. Эндпоинт /markets НЕ используется:
по решению проекта (probe 2026-07-31) он молча игнорирует tag_slug.

Статус знания (свежие пробы 2026-08-02, ответы цитируются в тестах):
- ФАКТ: /events?tag_slug=crypto&closed=false возвращает JSON-список событий,
  у события вложенные рынки в поле "markets" (keys см. тесты).
- ФАКТ: tag_slug -- слаг ТЕГА, а не рынка; tag_slug=btc-updown-5m пуст.
- ФАКТ: tag_slug=nosuchtagxyz возвращает пустой массив (параметр фильтрует);
  иначе -- TagFilterIgnored и остановка (эндпоинты проекта дважды молча
  игнорировали фильтры).
- ФАКТ: слаг up/down-рынка имеет вид "<coin>-updown-5m-<epoch>".
- ФАКТ: clobTokenIds -- JSON-строка массива из двух строк (token_id исходов);
  outcomes -- тоже JSON-строка массива, порядок совпадает с clobTokenIds
  (напр. '["Up", "Down"]'). Маска "updown" в слаге ловит и 5m, и 15m рынки.
- ФАКТ: пагинация /events через offset, limit упирается в 100; список НЕ
  сортирован по свежести, живые up/down рынки встречаются до offset=2000+
  (offset=1990 всё ещё отдавал 100 событий).
- ФАКТ: потолок offset=2000 (2001 -> 422) делает ПОЛНОЕ перечисление
  событий crypto невозможным; поэтому обязателен датный срез.
- ФАКТ: параметры end_date_min/end_date_max РЕАЛЬНО фильтруют серверно:
  окно вокруг now дало 14 событий, окно в 2020 году -- 0 событий
  (проверка ниже, та же болезнь молчаливого игнорирования фильтров).
- ФАКТ: при closed=false события приходят с closed=false; живой up/down рынок
  несёт acceptingOrders=True (14/14 в пробе).
- ПРЕДПОЛОЖЕНИЕ: датный срез [now-15m, now+15m] покрывает все живые up/down
  рынки (рынки живут 5 минут, align по 5-минутным границам). Список актуален
  только в момент запроса и заранее не сохраняется.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

import httpx

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
EVENTS_PATH = "/events"
TAG_CRYPTO = "crypto"
UPDOWN_MARKER = "updown"
PAGE_LIMIT = 100
OFFSET_CEILING = 2000
NONEXISTENT_TAG = "nosuchtagxyz"
USER_AGENT = "pm-validate/0.1 (personal research)"
WINDOW_LOOKBACK_MIN = 15
WINDOW_AHEAD_MIN = 15

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 15.0


class TagFilterIgnored(RuntimeError):
    """Параметр фильтра молча игнорируется сервером."""


@dataclass(frozen=True)
class UpdownOutcome:
    """Один исход рынка up/down: слаг рынка, имя исхода и его token_id."""

    market_slug: str
    outcome: str
    token_id: str
    coin: str
    interval_epoch: int | None


@dataclass(frozen=True)
class DiscoveryResult:
    """Результат обнаружения с диагностикой охвата."""

    outcomes: tuple[UpdownOutcome, ...]
    n_events_seen: int
    n_updown_markets: int


def _get_json(
    client: httpx.Client, path: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """GET c retry на 429/5xx; пустое тело -- данные, а не ошибка."""
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            r = client.get(path, params=params)
        except httpx.HTTPError as exc:
            last_err = exc
            time.sleep(min(_BASE_DELAY_S * (2**attempt), _MAX_DELAY_S))
            continue
        if r.status_code in _RETRY_STATUS and attempt < _RETRY_ATTEMPTS - 1:
            last_err = RuntimeError(f"HTTP {r.status_code} (retryable)")
            time.sleep(min(_BASE_DELAY_S * (2**attempt), _MAX_DELAY_S))
            continue
        if r.status_code >= 400:
            raise RuntimeError(
                f"GET {r.request.url} -> HTTP {r.status_code}: {r.text[:300]!r}"
            )
        try:
            payload = r.json()
        except ValueError as exc:
            raise RuntimeError(
                f"GET {r.request.url} -> не JSON: {r.text[:300]!r}"
            ) from exc
        if not isinstance(payload, list):
            raise RuntimeError(
                f"GET {r.request.url} -> неожиданная форма ответа: "
                f"{type(payload).__name__}"
            )
        return payload
    raise RuntimeError(f"GET {path} попытки исчерпаны: {last_err!r}")


def _fmt_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_events(
    client: httpx.Client,
    *,
    tag_slug: str,
    closed: bool | None = None,
    end_date_min: datetime | None = None,
    end_date_max: datetime | None = None,
    limit: int = PAGE_LIMIT,
) -> Iterator[dict[str, Any]]:
    """Ленивый обход /events с реальной (проверенной) фильтрацией.

    ДАТНЫЙ СРЕЗ ОБЯЗАТЕЛЕН: /events не сортирован по свежести, живые up/down
    рынки встречаются до offset=2000+, а потолок offset=2000 делает полное
    перечисление невозможным. end_date_min/max сужают до живого окна
    (факт пробы: окно в 2020 году вернуло 0 событий).

    Поднимает TagFilterIgnored при нарушении контракта фильтров:
    - несуществующий тег вернул данные;
    - при closed=False пришло событие с closed=True;
    - endDate события вне запрошенного окна (с допуском 60 c).
    """
    if not tag_slug:
        raise ValueError("iter_events требует непустой tag_slug")
    if (end_date_min is not None) != (end_date_max is not None):
        raise ValueError("end_date_min и end_date_max задаются только парой")
    lo = _fmt_iso(end_date_min) if end_date_min else None
    hi = _fmt_iso(end_date_max) if end_date_max else None
    offset = 0
    while True:
        if offset >= OFFSET_CEILING:
            raise RuntimeError(
                f"/events упёрся в потолок offset={OFFSET_CEILING}: "
                "охват up/down занижен"
            )
        params: dict[str, Any] = {"tag_slug": tag_slug, "limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if lo is not None:
            params["end_date_min"] = lo
            params["end_date_max"] = hi
        page = _get_json(client, EVENTS_PATH, params)
        if not page:
            return
        for event in page:
            ev_closed = event.get("closed")
            if closed is False and ev_closed is True:
                raise TagFilterIgnored(
                    f"closed=false запрошен, но событие {event.get('slug')!r} "
                    "вернулось с closed=true: параметр closed молча игнорируется"
                )
            if end_date_min is not None:
                ed = event.get("endDate")
                if isinstance(ed, str):
                    try:
                        ed_dt = _parse_iso(ed)
                    except ValueError:
                        ed_dt = None
                    inside = lo <= ed <= hi
                    in_tol = (
                        ed_dt is not None
                        and end_date_max is not None
                        and (end_date_min - timedelta(seconds=60))
                        <= ed_dt
                        <= (end_date_max + timedelta(seconds=60))
                    )
                    if not inside and not in_tol:
                        raise TagFilterIgnored(
                            f"endDate {ed!r} события {event.get('slug')!r} вне "
                            f"запрошенного окна [{lo}, {hi}]: параметр "
                            "end_date_min/max молча игнорируется"
                        )
            yield event
        if len(page) < limit:
            return
        offset += limit


def check_tag_filter(client: httpx.Client) -> bool:
    """Заведомо несуществующий тег должен дать пустой массив.

    Если вернулись данные -- параметр tag_slug игнорируется, работа останавли-
    вается (TagFilterIgnored).
    """
    page = _get_json(
        client, EVENTS_PATH, {"tag_slug": NONEXISTENT_TAG, "limit": PAGE_LIMIT}
    )
    if page:
        raise TagFilterIgnored(
            f"tag_slug={NONEXISTENT_TAG} вернул {len(page)} записей: параметр "
            "молча игнорируется сервером, отбор по тегу бессмыслен"
        )
    return True


def _token_ids(market: dict[str, Any]) -> list[str]:
    """clobTokenIds: JSON-строка массива строк либо уже список."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        v = raw
    if isinstance(v, list):
        return [str(x) for x in v if str(x)]
    return []


def _outcome_names(market: dict[str, Any]) -> list[str]:
    """outcomes: JSON-строка массива строк (факт пробы 2026-08-02) либо список."""
    raw = market.get("outcomes")
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        v = raw
    if isinstance(v, list):
        return [str(x) for x in v if str(x)]
    return []


def _coin_from_slug(slug: str) -> str:
    return slug.split("-updown-")[0]


def _epoch_from_slug(slug: str) -> int | None:
    tail = slug.rsplit("-", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def _parse_iso(value: str) -> datetime:
    """ISO-8601 -> aware datetime UTC (с или без 'Z')."""
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _is_live(market: dict[str, Any]) -> bool:
    """Живой = принимает ордера. acceptingOrders -- прямой сигнал; если его нет,
    fallback на активность и закрытость."""
    ao = market.get("acceptingOrders")
    if isinstance(ao, bool):
        return ao
    if market.get("closed") is True:
        return False
    return market.get("active") is True


def updown_outcomes(
    client: httpx.Client,
    *,
    require_live: bool = True,
    now: datetime | None = None,
) -> DiscoveryResult:
    """Собирает живые up/down рынки и исходы с token_id в текущем окне.

    ДАТНЫЙ СРЕЗ: без него /events тянет >2000 событий crypto до потолка
    offset=2000, а список не сортирован по свежести. Окно [now-15m, now+15m]
    покрывает все живые 5-минутные рынки.

    require_live=True: неживые рынки (acceptingOrders=false, closed, активные
    в прошлом) отбрасываются; счётчик n_updown_markets считает ВСЕ up/down
    рынки окна, чтобы видеть, сколько отброшено.
    """
    now = now or datetime.now(timezone.utc)
    lo = now - timedelta(minutes=WINDOW_LOOKBACK_MIN)
    hi = now + timedelta(minutes=WINDOW_AHEAD_MIN)
    outcomes: list[UpdownOutcome] = []
    n_events_seen = 0
    n_updown_markets = 0
    for event in iter_events(
        client,
        tag_slug=TAG_CRYPTO,
        closed=False,
        end_date_min=lo,
        end_date_max=hi,
    ):
        n_events_seen += 1
        markets = event.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            slug = market.get("slug")
            if not isinstance(slug, str) or UPDOWN_MARKER not in slug:
                continue
            n_updown_markets += 1
            if require_live and not _is_live(market):
                continue
            tokens = _token_ids(market)
            names = _outcome_names(market)
            if not tokens:
                continue
            for i, tid in enumerate(tokens):
                if isinstance(names, list) and i < len(names):
                    name = str(names[i])
                else:
                    name = f"outcome{i}"
                outcomes.append(
                    UpdownOutcome(
                        market_slug=slug,
                        outcome=name,
                        token_id=tid,
                        coin=_coin_from_slug(slug),
                        interval_epoch=_epoch_from_slug(slug),
                    )
                )
    return DiscoveryResult(
        outcomes=tuple(outcomes),
        n_events_seen=n_events_seen,
        n_updown_markets=n_updown_markets,
    )


def _slug_counts(result: DiscoveryResult) -> dict[str, int]:
    """Слаг -> число исходов (token_id)."""
    counts: dict[str, int] = {}
    for o in result.outcomes:
        counts[o.market_slug] = counts.get(o.market_slug, 0) + 1
    return counts


def print_report(result: DiscoveryResult, *, tag_check_ok: bool) -> None:
    """Печатает отчёт задачи 1."""
    counts = _slug_counts(result)
    print("=== ЗАДАЧА 1. ОБНАРУЖЕНИЕ РЫНКОВ UP/DOWN ===")
    print(
        f"Проверка несуществующим тегом tag_slug={NONEXISTENT_TAG}: "
        f"{'OK, пустой массив (параметр фильтрует)' if tag_check_ok else 'FAIL'}"
    )
    print(f"Событий просмотрено: {result.n_events_seen}")
    print(f"Рынков с маской 'updown' в слаге: {result.n_updown_markets}")
    print(f"Живых исходов (слаг + token_id): {len(result.outcomes)}")
    print("Слаги (число исходов):")
    for slug in counts:
        print(f"  {slug}: {counts[slug]}")
    print("Исходы (слаг | исход | token_id):")
    for o in result.outcomes:
        print(f"  {o.market_slug} | {o.outcome} | {o.token_id}")


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа: проверка фильтра + обнаружение живых up/down рынков."""
    with httpx.Client(
        base_url=GAMMA_BASE_URL,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        tag_check_ok = check_tag_filter(client)
        result = updown_outcomes(client)
    print_report(result, tag_check_ok=tag_check_ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
