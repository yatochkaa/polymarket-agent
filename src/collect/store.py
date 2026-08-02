"""Хранение коллектора: duckdb как движок запросов + parquet-проекция.

Схемы берутся из schema.py (единственный источник). INSERT OR IGNORE с
естественным ключом даёт идемпотентность: повторный запуск не дублирует строки.

Parquet: сырой архив, партиция по дате (UTC). Для сверки с pmdata
экспортируется ПРОЕКЦИЯ из 12 колонок замороженной схемы (см. schema.py).

Единицы времени везде: epoch milliseconds, int64.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from . import schema

DEFAULT_DB_PATH = Path("data") / "pm.duckdb"
DEFAULT_EXPORT_ROOT = Path("data") / "collect"


def _columns_ddl(cols: dict[str, str]) -> str:
    return ",\n    ".join(f"{name} {typ}" for name, typ in cols.items())


def build_ddl() -> str:
    """DDL всех таблиц коллектора из schema.TABLES."""
    parts: list[str] = []
    for table, (cols, key) in schema.TABLES.items():
        key_sql = ", ".join(key)
        parts.append(
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            f"    {_columns_ddl(cols)},\n"
            f"    PRIMARY KEY ({key_sql})\n"
            f");"
        )
    return "\n".join(parts)


def connect(db_path: Path = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Открывает duckdb и гарантирует схему коллектора."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(build_ddl())
    return con


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [table]
    ).fetchone()
    return row is not None


def current_seq(con: duckdb.DuckDBPyConnection, table: str, token_id: str) -> int:
    """Следующее значение seq для token_id (продолжение после рестарта)."""
    row = con.execute(
        f"SELECT COALESCE(MAX(seq), 0) FROM {table} WHERE token_id = ?",
        [token_id],
    ).fetchone()
    return int(row[0]) + 1


def row_exists(
    con: duckdb.DuckDBPyConnection,
    table: str,
    *,
    token_id: str,
    seq: int,
) -> bool:
    """Есть ли строка с естественным ключом (для контроля seq)."""
    row = con.execute(
        f"SELECT 1 FROM {table} WHERE token_id = ? AND seq = ?",
        [token_id, seq],
    ).fetchone()
    return row is not None


def insert_row(
    con: duckdb.DuckDBPyConnection,
    table: str,
    row: dict[str, Any],
) -> bool:
    """INSERT OR IGNORE одной строки. True, если строка реально вставлена."""
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    sql = f"INSERT OR IGNORE INTO {table} ({col_sql}) VALUES ({placeholders})"
    con.execute(sql, [row[c] for c in cols])
    # Ключи во всех таблицах NOT NULL -> безопасно сравнивать через '='.
    cur = con.execute(
        f"SELECT 1 FROM {table} WHERE { ' AND '.join(f'{c} = ?' for c in schema.TABLES[table][1]) }",
        [row[c] for c in schema.TABLES[table][1]],
    )
    return cur.fetchone() is not None


def upsert_market(
    con: duckdb.DuckDBPyConnection,
    market: dict[str, Any],
) -> None:
    """markets_tracked: INSERT OR IGNORE; при конфликте не трогаем (первая запись)."""
    cols = list(market.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    con.execute(
        f"INSERT OR IGNORE INTO markets_tracked ({col_sql}) VALUES ({placeholders})",
        [market[c] for c in cols],
    )


def update_market_field(
    con: duckdb.DuckDBPyConnection,
    token_id: str,
    **fields: Any,
) -> None:
    """Дозаполнение полей markets_tracked (например, market_id по первому WS)."""
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    con.execute(
        f"UPDATE markets_tracked SET {sets} WHERE token_id = ?",
        [*fields.values(), token_id],
    )


def start_session(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str,
    git_commit: str | None,
    markets_subscribed: int,
    now_ms: int,
) -> None:
    con.execute(
        "INSERT OR IGNORE INTO collector_sessions "
        "(session_id, started_ms, git_commit, markets_subscribed) VALUES (?, ?, ?, ?)",
        [session_id, now_ms, git_commit, markets_subscribed],
    )


def end_session(
    con: duckdb.DuckDBPyConnection,
    *,
    session_id: str,
    exit_reason: str,
    now_ms: int,
) -> None:
    con.execute(
        "UPDATE collector_sessions SET ended_ms = ?, exit_reason = ? "
        "WHERE session_id = ?",
        [now_ms, exit_reason, session_id],
    )


def last_session_bounds(con: duckdb.DuckDBPyConnection) -> tuple[int | None, int | None]:
    """(started_ms, ended_ms) последней сессии до текущей (для process_restart)."""
    row = con.execute(
        "SELECT started_ms, ended_ms FROM collector_sessions "
        "WHERE ended_ms IS NOT NULL ORDER BY started_ms DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = con.execute(
            "SELECT started_ms, NULL FROM collector_sessions "
            "ORDER BY started_ms DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return (None, None)
    return (int(row[0]), int(row[1]) if row[1] is not None else None)


def tracked_tokens(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute("SELECT token_id FROM markets_tracked ORDER BY start_ms").fetchall()
    return [str(r[0]) for r in rows]


def export_book_projection(
    con: duckdb.DuckDBPyConnection,
    out_path: Path,
    *,
    day: str,
) -> None:
    """Экспорт book_snapshots за день в parquet: ПРОЕКЦИЯ из 12 колонок.

    Используется сверялкой src/validate/compare.py. Только source='ws':
    rest_backfill внутри разрыва наблюдением не считается.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(schema.BOOK_EXPORT_SCHEMA.names)
    select_sql = ", ".join(cols)
    rows = con.execute(
        f"""
        SELECT {select_sql} FROM book_snapshots
        WHERE source = ?
          AND strftime(to_timestamp(ts_recv_ms / 1000.0), '%Y-%m-%d') = ?
        ORDER BY token_id, seq
        """,
        [schema.SOURCE_WS, day],
    ).fetchall()
    records = [dict(zip(cols, r)) for r in rows]
    table = pa.Table.from_pylist(records, schema=schema.BOOK_EXPORT_SCHEMA)
    pq.write_table(table, out_path)


def export_tables(
    con: duckdb.DuckDBPyConnection,
    out_root: Path = DEFAULT_EXPORT_ROOT,
    *,
    day: str | None = None,
) -> dict[str, Path | None]:
    """Партиционированный по дате экспорт: book_snapshots (проекция),
    gap_intervals, recon_checks. Идемпотентно: файл дня перезаписывается.

    Returns:
        {имя_таблицы: путь} для непустых экспортов.
    """
    if day is None:
        row = con.execute("SELECT COALESCE(MAX(ts_recv_ms), 0) FROM book_snapshots").fetchone()
        max_ms = int(row[0])
        day = datetime.fromtimestamp(max_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_root = Path(out_root)
    written: dict[str, Path | None] = {}
    snap_path = out_root / "book_snapshots" / day / "snapshots.parquet"
    export_book_projection(con, snap_path, day=day)
    written["book_snapshots"] = snap_path
    for table in ("gap_intervals", "recon_checks"):
        path = out_root / table / day / f"{table}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = list(schema.TABLES[table][0].keys())
        select_sql = ", ".join(cols)
        order_col = "start_ms" if table == "gap_intervals" else "ts_recv_ms"
        rows = con.execute(
            f"SELECT {select_sql} FROM {table} ORDER BY token_id, {order_col}"
        ).fetchall()
        if not rows:
            written[table] = None
            continue
        records = [dict(zip(cols, r)) for r in rows]
        pq.write_table(pa.Table.from_pylist(records), path)
        written[table] = path
    return written


def count_rows(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {t: int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in schema.TABLES}


def utc_day_stamp(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
