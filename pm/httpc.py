"""HTTP-клиент для чтения публичных эндпоинтов Polymarket.

Ответственность: retry с backoff, rate limit, единый журнал сырых ответов.
Никакой деловой логики здесь нет.

Важно для Э1: пустой ответ -- это ДАННЫЕ, а не ошибка. /orderbook-history
молча возвращает пустоту с 20.02.2026, и любой другой эндпоинт может повторить
этот режим отказа. Поэтому клиент всегда возвращает Envelope с метаданными
(статус, длина тела, признак пустоты), а не сразу payload.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpFailure(RuntimeError):
    """Невосстановимая ошибка запроса после всех попыток."""

    def __init__(self, url: str, status: int | None, detail: str) -> None:
        super().__init__(f"{url} -> status={status} detail={detail[:400]}")
        self.url = url
        self.status = status
        self.detail = detail


@dataclass(slots=True)
class Envelope:
    """Сырой результат запроса с метаданными для аудита."""

    url: str
    status: int
    elapsed_ms: float
    payload: Any
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True для None, [], {} и {"history": []} -- типичных тихих отказов."""
        p = self.payload
        if p is None:
            return True
        if isinstance(p, (list, str)):
            return len(p) == 0
        if isinstance(p, dict):
            if not p:
                return True
            inner = p.get("history")
            if isinstance(inner, list):
                return len(inner) == 0
        return False


class ReadClient:
    """Синхронный read-only клиент. Не умеет делать POST/DELETE по дизаину:
    запись живёт только в pm.experiments.e3_gtd_cancel и требует подтверждения.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 20.0,
        max_retries: int = 4,
        rate_limit_rps: float = 4.0,
        raw_log: Path | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"User-Agent": "pm-recon/0.1 (personal research)"},
            follow_redirects=True,
        )
        self._max_retries = max_retries
        self._min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self._last_call = 0.0
        self._raw_log = raw_log

    def __enter__(self) -> "ReadClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрывает пул соединений."""
        self._client.close()

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Envelope:
        """GET с retry. Не бросает исключение на пустое тело.

        Args:
            path: путь относительно base_url, например "/prices-history".
            params: query-параметры.

        Returns:
            Envelope с распарсенным JSON или сырой строкой в payload.

        Raises:
            HttpFailure: если все попытки закончились сетевой ошибкой или 5xx/429.
        """
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_detail = ""
        last_status: int | None = None
        for attempt in range(self._max_retries + 1):
            self._throttle()
            t0 = time.perf_counter()
            try:
                r = self._client.get(path, params=clean)
            except httpx.HTTPError as exc:
                last_detail = repr(exc)
                last_status = None
                log.warning("GET %s attempt %d network error: %s", path, attempt, exc)
                time.sleep(min(2**attempt, 15))
                continue
            elapsed = (time.perf_counter() - t0) * 1000
            if r.status_code in RETRY_STATUS and attempt < self._max_retries:
                last_status, last_detail = r.status_code, r.text
                time.sleep(min(2**attempt, 15))
                continue
            if r.status_code >= 400:
                raise HttpFailure(str(r.request.url), r.status_code, r.text)
            try:
                payload = r.json()
            except ValueError:
                payload = r.text
            env = Envelope(
                url=str(r.request.url),
                status=r.status_code,
                elapsed_ms=elapsed,
                payload=payload,
                params=dict(clean),
            )
            self._append_raw(env)
            return env
        raise HttpFailure(path, last_status, last_detail or "retries exhausted")

    def _append_raw(self, env: Envelope) -> None:
        if self._raw_log is None:
            return
        self._raw_log.parent.mkdir(parents=True, exist_ok=True)
        with self._raw_log.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "url": env.url,
                        "status": env.status,
                        "elapsed_ms": round(env.elapsed_ms, 2),
                        "empty": env.is_empty,
                        "payload": env.payload,
                    },
                    default=str,
                )
                + "\n"
            )
