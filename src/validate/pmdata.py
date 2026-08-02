"""Задача 3: внешний эталон pmdata.dev.

Качает ОДИН слаг 15-минутного up/down рынка (data_type=poly_l2),
окно которого закрылось ~2 часа назад, и печатает факты о файле.

Ключ читается ТОЛЬКО из .env под именем PMDATA_API_KEY. В код не
зашивается, в чат не печатается, в git не коммитится.

Если ключ не работает или сервис отвечает ошибкой -- печатается код
и тело ответа, работа останавливается. Обходные пути не пробуются,
схема не выдумывается.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

DEFAULT_GAMMA = "https://gamma-api.polymarket.com"
PAGES_PATH = "/events"
DEFAULT_PMDATA = "https://api.pmdata.dev"
DOWNLOAD_PATH = "/download/poly_l2/{slug}.parquet"
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "validate"
TAG_CRYPTO = "crypto"
WINDOW_S = 900
CLOSED_AGO_S = 2 * 3600
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _load_env_var(name: str) -> str | None:
    """Вернуть значение переменной из окружения, затем из .env (без вывода)."""
    value = __import__("os").environ.get(name)
    if value is not None:
        return value.strip().strip('"').strip("'")
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return None


def _slug_epoch() -> int:
    """Начало 15-минутного окна, закрывшегося ~2 часа назад (арифметика)."""
    now = time.time()
    return int((now - CLOSED_AGO_S) // WINDOW_S) * WINDOW_S


def _fmt_iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _verify_market(client: httpx.Client, epoch: int) -> str:
    """Проверить через Gamma, что up/down-15m рынок с этой эпохой существовал."""
    lo = _fmt_iso(epoch - 60)
    hi = _fmt_iso(epoch + WINDOW_S + 120)
    params = {
        "tag_slug": TAG_CRYPTO,
        "end_date_min": lo,
        "end_date_max": hi,
        "limit": 100,
        "offset": 0,
    }
    r = client.get(DEFAULT_GAMMA + PAGES_PATH, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(
            f"Gamma /events вернул {r.status_code}: {r.text[:300]}"
        )
    slug_tail = f"updown-15m-{epoch}"
    for event in r.json():
        for market in event.get("markets", []):
            slug = market.get("slug", "")
            if slug.endswith(slug_tail):
                return slug
    raise RuntimeError(
        f"Рынок *-updown-15m-{epoch} не найден в Gamma в окне [{lo}, {hi}]"
    )


def _download(client: httpx.Client, slug: str, api_key: str) -> Path:
    """Скачать poly_l2 parquet для слага в data/validate/."""
    url = DEFAULT_PMDATA + DOWNLOAD_PATH.format(slug=slug)
    headers = {"api_key": api_key, "User-Agent": "Mozilla/5.0"}
    r = client.get(url, headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(
            f"pmdata.dev вернул {r.status_code}: {r.text[:300]}"
        )
    out = DATA_DIR / f"{slug}.parquet"
    out.write_bytes(r.content)
    return out


def _print_facts(path: Path) -> None:
    """Напечатать факты о скачанном файле без домыслов."""
    table = pq.read_table(path)
    print("columns:")
    for field in table.schema:
        print(f"  {field.name}: {field.type}")
    n = table.num_rows
    print(f"rows: {n}")
    print("first 5 rows:")
    for row in table.slice(0, 5).to_pylist():
        print("  " + repr(row))
    for col in table.column_names:
        lowered = col.lower()
        if any(tok in lowered for tok in ("time", "ts_", "date", "timestamp")):
            arr = table.column(col)
            mn = pc.min(arr).as_py()
            mx = pc.max(arr).as_py()
            print(f"timestamp col {col!r}: min={mn!r} max={mx!r} arrow={arr.type}")
            unit = "?"  # единицы определяются по факту, не домысляются
            if isinstance(mn, (int, float)) and isinstance(mx, (int, float)):
                if 1e12 <= mn < 1e13:  # факт: 13-значное, похоже на мс эпохи
                    unit = "epoch milliseconds (13-digit)"
                elif 1e9 <= mn < 1e10:
                    unit = "epoch seconds (10-digit)"
            print(f"  unit guess by magnitude: {unit}")
    print("event_type values:")
    if "event_type" in table.column_names:
        counts: dict[str, int] = {}
        for v in table.column("event_type").to_pylist():
            counts[str(v)] = counts.get(str(v), 0) + 1
        for k, v in sorted(counts.items()):
            print(f"  {k!r}: {v}")


def main() -> int:
    api_key = _load_env_var("PMDATA_API_KEY")
    if not api_key:
        print("STOP: PMDATA_API_KEY не найден (env или .env).")
        print("      Заведи ключ на pmdata.dev и положи в .env: PMDATA_API_KEY=...")
        return 1
    epoch = _slug_epoch()
    print(f"slug epoch: {epoch} ({_fmt_iso(epoch)} UTC, окно закрылось ~2ч назад)")
    with httpx.Client() as client:
        slug = _verify_market(client, epoch)
        print(f"market verified via gamma: {slug}")
        path = _download(client, slug, api_key)
    print(f"downloaded: {path} ({path.stat().st_size} bytes)")
    _print_facts(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
