"""Персистентность: parquet для сырых выгрузок + duckdb для агрегатов.

Принцип: сырые данные неизменяемы и пишутся раз (append-only файлы с меткой
времени в имени). Любой пересчёт выводов должен воспроизводиться из них.
История стакана существует только та, что собрали мы -- терять её нельзя.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)


def utc_stamp() -> str:
    """Метка времени UTC, безопасная для имён файлов."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, obj: Any) -> Path:
    """Пишет JSON с созданием родительских каталогов."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def write_parquet(path: Path, rows: Sequence[dict[str, Any]]) -> Path | None:
    """Пишет список словарей в parquet.

    Args:
        path: целевой файл .parquet.
        rows: однородные записи.

    Returns:
        Путь к файлу либо None, если rows пуст.

    Raises:
        ImportError: если pyarrow не установлен (тогда используйте write_json).
    """
    if not rows:
        log.warning("write_parquet: пустой набор, файл не создан: %s", path)
        return None
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([dict(r) for r in rows])
    pq.write_table(table, path)
    return path


DDL = """
CREATE TABLE IF NOT EXISTS e1_points (
    token_id      TEXT NOT NULL,
    condition_id  TEXT,
    ts            BIGINT NOT NULL,
    price         DOUBLE NOT NULL,
    fidelity      INTEGER,
    fetched_at    TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS e1_pair_sums (
    condition_id  TEXT NOT NULL,
    ts            BIGINT NOT NULL,
    sum_price     DOUBLE NOT NULL,
    dev           DOUBLE NOT NULL
);
CREATE TABLE IF NOT EXISTS e4_tennis_markets (
    condition_id  TEXT,
    slug          TEXT,
    volume_24h    DOUBLE,
    closed        BOOLEAN,
    uma_disputed  BOOLEAN,
    resolved_at   TIMESTAMP,
    fetched_at    TIMESTAMP NOT NULL
);
"""


def connect(db_path: Path):
    """Открывает duckdb и гарантирует схему.

    Args:
        db_path: файл базы (создаётся при отсутствии).

    Returns:
        duckdb.DuckDBPyConnection.

    Raises:
        ImportError: если duckdb не установлен.
    """
    import duckdb

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(DDL)
    return con
