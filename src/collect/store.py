"""Хранение коллектора: duckdb как движок запросов + parquet-проекция.

Схемы берутся из schema.py (единственный источник). INSERT OR IGNORE с
естественным ключом даёт идемпотентность: повторный запуск не дублирует строки.

Parquet: сырой архив, партиция по дате (UTC). Для сверки с pmdata
экспортируется ПРОЕКЦИЯ из 12 колонок замороженной схемы (см. schema.py).

Единицы времени везде: epoch milliseconds, int64.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import duckdb
import pyarrow as pa

from . import schema

log = logging.getLogger("collect.store")

# Маппинг duckdb-типов замороженной схемы на типы pyarrow для пачечной
# вставки через Arrow (см. StoreWriter._flush). duckdb.executemany со
# списком списков делает ленивый `import pandas` и при его отсутствии
# зацикливается — поэтому пишем через Arrow, а не executemany.
_PA_TYPE_MAP: dict[str, pa.DataType] = {
    "BIGINT": pa.int64(),
    "INTEGER": pa.int32(),
    "DOUBLE": pa.float64(),
    "TEXT": pa.string(),
    "BOOLEAN": pa.bool_(),
}

_TMP_TABLE = "__collector_flush"


def _pa_type_for(col_type: str) -> pa.DataType | None:
    """Тип pyarrow для duckdb-типа колонки (базовое слово до NOT NULL)."""
    base = col_type.split()[0]
    return _PA_TYPE_MAP.get(base)

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


def _column_type_for_add(col_type: str) -> str:
    """Тип колонки для ALTER TABLE ... ADD COLUMN: duckdb не принимает
    constraints в ADD COLUMN (ParserException 'Adding columns with constraints
    not yet supported'). В schema.py типы — слово + опциональный ' NOT NULL';
    constraint отрезаем, само слово типа сохраняется."""
    t = col_type.strip()
    suffix = " NOT NULL"
    if t.upper().endswith(suffix):
        t = t[: -len(suffix)].rstrip()
    return t


def _create_table_sql(table: str, cols: dict[str, str], key: tuple[str, ...]) -> str:
    """CREATE TABLE ... (без IF NOT EXISTS) для одной таблицы."""
    key_sql = ", ".join(key)
    return (
        f"CREATE TABLE {table} (\n"
        f"    {_columns_ddl(cols)},\n"
        f"    PRIMARY KEY ({key_sql})\n"
        f");"
    )


def actual_primary_key(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Колонки фактического первичного ключа таблицы (в порядке позиций)."""
    rows = con.execute(
        "SELECT column_name FROM information_schema.key_column_usage "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [str(r[0]) for r in rows]


PRE_SEQ_EXPORT_NAME = "recon_checks_pre_seq.parquet"


def _recon_pre_seq_path(db_path: Path) -> Path:
    """Путь выгрузки строк recon_checks с seq IS NULL.

    Для живой базы data/pm.duckdb это ровно data/validate/recon_checks_pre_seq.parquet.
    Вывод базируется на каталоге самой базы, чтобы тесты на временных базах не
    писали в рабочий каталог проекта.
    """
    return Path(db_path).parent / "validate" / PRE_SEQ_EXPORT_NAME


def _dump_recon_pre_seq(
    con: duckdb.DuckDBPyConnection,
    path: Path,
) -> tuple[int, dict[str, int]]:
    """Выгружает все строки recon_checks с seq IS NULL в parquet «как есть».

    Returns:
        (число строк, разбивка по verdict).
    Raises:
        FileExistsError: файл уже существует — молча не перезаписываем.
        Exception: любая ошибка выгрузки или несовпадение числа строк.
    """
    if path.exists():
        raise FileExistsError(
            f"выгрузка recon_checks.pre_seq: файл уже существует ({path}) — "
            "запрещено перезаписывать молча, миграция не выполняется"
        )
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols = list(schema.RECON_CHECKS_COLUMNS.keys())
    col_sql = ", ".join(cols)
    rows = con.execute(
        f"SELECT {col_sql} FROM recon_checks WHERE seq IS NULL"
    ).fetchall()
    n = len(rows)
    verdict_idx = cols.index("verdict")
    breakdown: dict[str, int] = {}
    for r in rows:
        v = str(r[verdict_idx])
        breakdown[v] = breakdown.get(v, 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        table = pa.Table.from_pylist([dict(zip(cols, r)) for r in rows])
        pq.write_table(table, path)
    except Exception:
        log.exception("выгрузка recon_checks.pre_seq не удалась: %s", path)
        raise
    written = pq.ParquetFile(path).metadata.num_rows
    if written != n:
        raise RuntimeError(
            f"контроль выгрузки recon_checks.pre_seq: в файле {written} строк, "
            f"ожидалось {n} — миграция прервана"
        )
    return n, breakdown


def _ensure_recon_checks_key(
    con: duckdb.DuckDBPyConnection,
    pre_seq_path: Path,
) -> None:
    """Пересоздаёт recon_checks с первичным ключом (token_id, seq).

    Живая таблица (data/pm.duckdb) была создана с ПК (token_id, ts_recv_ms),
    а не (token_id, seq) — из-за расхождения с schema.TABLES пачка строк с
    разными seq в одну миллисекунду проходила внутренний дедуп, но на уровне
    БД конфликтовала, INSERT OR IGNORE бросал ConstraintException, транзакция
    откатывалась, и _flush_guarded молча терял ВЕСЬ пакет (Дефект А).

    CREATE TABLE IF NOT EXISTS не меняет ПК существующей таблицы, поэтому для
    расхождения выполняем явную миграцию: новая таблица -> перенос строк ->
    переименование. Строки с seq IS NOT NULL сохраняются (первая по ключу).

    Строки с seq = NULL НЕ могут попасть под NOT NULL ПК (token_id, seq) и
    были бы потеряны — поэтому ПЕРЕД пересозданием они в обязательном порядке
    выгружаются в pre_seq_path (со всеми колонками «как есть»). Порядок:
      1. если файл уже существует — громкий отказ (не перезаписываем молча);
      2. выгрузка; при любой ошибке выгрузки миграция НЕ выполняется;
      3. контроль числа строк в файле == числу строк с seq IS NULL;
      4. только после этого — пересоздание таблицы.
    Число выгруженных строк и разбивка по verdict логируются.
    """
    cols, key = schema.TABLES["recon_checks"]
    expected = list(key)  # ("token_id", "seq")
    existing_exists = table_exists(con, "recon_checks")
    if existing_exists and actual_primary_key(con, "recon_checks") == expected:
        return

    new_table = "__recon_checks_new"
    con.execute(f"DROP TABLE IF EXISTS {new_table}")
    con.execute(_create_table_sql(new_table, cols, key))
    col_sql = ", ".join(cols)
    if existing_exists and count_rows_table(con, "recon_checks") > 0:
        n_null = int(
            con.execute(
                f"SELECT COUNT(*) FROM recon_checks WHERE {key[1]} IS NULL"
            ).fetchone()[0]
        )
        if n_null > 0:
            n_dump, verdict_breakdown = _dump_recon_pre_seq(con, pre_seq_path)
            log.info(
                "recon_checks: выгружено строк с seq IS NULL: %d, "
                "по verdict: %s (файл %s)",
                n_dump,
                verdict_breakdown,
                pre_seq_path,
            )
        else:
            log.info("recon_checks: строк с seq IS NULL нет — выгрузка не требуется")
        key_cols_sql = ", ".join(key)
        con.execute(
            f"""
            INSERT INTO {new_table} ({col_sql})
            SELECT {col_sql} FROM (
                SELECT {col_sql},
                       row_number() OVER (
                           PARTITION BY {key_cols_sql}
                           ORDER BY {key_cols_sql}
                       ) AS __rn
                FROM recon_checks
                WHERE {key[1]} IS NOT NULL
            ) WHERE __rn = 1
            """
        )
        con.execute("DROP TABLE recon_checks")
        con.execute(f"ALTER TABLE {new_table} RENAME TO recon_checks")
        log.warning(
            "миграция: recon_checks пересоздана с primary key (%s), "
            "строки с seq NOT NULL сохранены, seq IS NULL выгружены в %s",
            ", ".join(key),
            pre_seq_path,
        )
    else:
        con.execute(f"ALTER TABLE {new_table} RENAME TO recon_checks")
        log.warning(
            "миграция: recon_checks создана с primary key (%s)",
            ", ".join(key),
        )


def count_rows_table(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def migrate_schema(
    con: duckdb.DuckDBPyConnection,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Добавляет отсутствующие колонки существующих таблиц.

    CREATE TABLE IF NOT EXISTS создаёт таблицу только целиком и НЕ добавляет
    колонки к уже созданной. recon_checks в data/pm.duckdb создана до коммита
    6ba0e29 (без колонки seq): export_tables и суточная сводка падали на
    SELECT seq по 1051 разу за прогон. Здесь фактический набор колонок каждой
    таблицы сверяется с ожидаемым (schema.TABLES), недостающие добавляются
    через ALTER TABLE ... ADD COLUMN. Тип constraints теряется (см.
    _column_type_for_add): существующие строки получают NULL.

    db_path нужен для вывода выгрузки строк с seq IS NULL (путь рядом с базой).
    """
    for table, (cols, _key) in schema.TABLES.items():
        if not table_exists(con, table):
            continue
        actual = {
            str(row[0]) for row in con.execute(f"DESCRIBE {table}").fetchall()
        }
        for col, col_type in cols.items():
            if col in actual:
                continue
            add_type = _column_type_for_add(col_type)
            con.execute(
                f"ALTER TABLE {table} ADD COLUMN {col} {add_type}"
            )
            log.warning("миграция: %s.%s добавлена (%s)", table, col, add_type)
    # Первичный ключ recon_checks обязан быть (token_id, seq): если существующая
    # таблица создана с другим ПК (например (token_id, ts_recv_ms)), CREATE TABLE
    # IF NOT EXISTS его не меняет, и пачка с разными seq в одну миллисекунду
    # откатывает всю транзакцию. Пересоздаём явной миграцией с сохранением строк
    # и выгрузкой строк с seq IS NULL.
    _ensure_recon_checks_key(con, _recon_pre_seq_path(db_path))


def connect(db_path: Path = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Открывает duckdb и гарантирует схему коллектора."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(build_ddl())
        migrate_schema(con, db_path)
    except Exception:
        # Windows держит файл залоченным, пока открыто соединение: при ошибке
        # миграции (например, файл выгрузки уже существует) закрываем, иначе
        # последующий close()/удаление каталога упрётся в PermissionError.
        con.close()
        raise
    return con


class StoreWriter:
    """Поток-писатель: приём только кладёт строки в очередь.

    Цикл событий не трогает duckdb: submit_row / submit_call не блокируют.
    Отдельный поток владеет соединением, копит строки и пишет их пачками
    в одной транзакции. call() — синхронное чтение/команда в этом потоке
    (для редких вызовов: старт/стоп сессии, экспорт, MAX(seq)).

    Гарантии:
    - порядок между строками и командами одного потока-отправителя
      сохраняется (единая FIFO-очередь);
    - перед любой командой (call/submit_call) накопленная пачка
      сбрасывается в базу, поэтому чтения видят предыдущие записи;
    - INSERT OR IGNORE + PRIMARY KEY даёт идемпотентность, как и раньше.

    Стойкость к потере пакета (Дефект А): _flush_guarded пробрасывает ошибку
    пачки наверх, поэтому поток-писатель МОЖЕТ умереть. Чтобы это не подвесило
    последующие call()/flush() на done.wait(), фатальная ошибка сохраняется в
    self._failed: _run будит активный sync и сбрасывает очередь (каждому
    ожидающему sync в error кладётся та же ошибка). call()/flush() тогда не
    зависают, а поднимают одну и ту же первопричину.
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        *,
        batch: int = 256,
        flush_interval_s: float = 0.1,
    ) -> None:
        self._db_path = Path(db_path)
        self._batch = batch
        self._queue: queue.Queue[Any] = queue.Queue()
        self._failed: Exception | None = None
        self._thread = threading.Thread(
            target=self._run, name="store-writer", daemon=True
        )
        self._thread.start()

    # ---- публичный интерфейс (не блокирует цикл событий) ----

    def submit_row(self, table: str, row: dict[str, Any]) -> None:
        """Очередная строка (INSERT OR IGNORE). Не блокирует."""
        self._queue.put(("row", table, row))

    def submit_call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Команда в потоке-писателе (например, export_tables). Не блокирует."""
        self._queue.put(("call", fn, args, kwargs))

    def call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Синхронно выполняет fn(con, *args, **kwargs) в потоке-писателе.
        Блокирует вызывающий поток. Ошибки внутри fn пробрасываются."""
        result: dict[str, Any] = {}
        done = threading.Event()
        self._queue.put(("sync", fn, args, kwargs, result, done))
        done.wait()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def flush(self) -> None:
        """Блокирует, пока все ранее поставленные строки не уйдут в базу."""
        self.call(lambda con: None)

    def close(self) -> None:
        """Сброс пачки, остановка потока, закрытие соединения."""
        self._queue.put(None)
        self._thread.join(timeout=30.0)

    # ---- поток-писатель ----

    def _run(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(self._db_path))
        con.execute(build_ddl())
        migrate_schema(con, self._db_path)
        active_sync: tuple[dict[str, Any], threading.Event] | None = None
        try:
            batch: list[tuple[str, dict[str, Any]]] = []
            while True:
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if batch:
                        self._flush_guarded(con, batch)
                        batch = []
                    continue
                if item is None:
                    self._flush_guarded(con, batch)
                    break
                op = item[0]
                if op == "row":
                    batch.append((item[1], item[2]))
                    if len(batch) >= self._batch:
                        self._flush_guarded(con, batch)
                        batch = []
                elif op == "call":
                    self._flush_guarded(con, batch)
                    batch = []
                    _, fn, args, kwargs = item
                    try:
                        fn(con, *args, **kwargs)
                    except Exception:  # noqa: BLE001 — поток должен жить
                        log.exception("store-writer: call %s не выполнен", fn)
                elif op == "sync":
                    self._flush_guarded(con, batch)
                    batch = []
                    _, fn, args, kwargs, result, done = item
                    active_sync = (result, done)
                    try:
                        result["value"] = fn(con, *args, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        result["error"] = exc
                    finally:
                        done.set()
                        active_sync = None
        except Exception as exc:  # фатально: потерян пакет (Дефект А)
            self._failed = exc
            log.error("store-writer: поток завершился из-за потери пакета: %s", exc)
            if active_sync is not None:
                result, done = active_sync
                result["error"] = exc
                done.set()
            self._drain_queue_abort(exc)
        finally:
            con.close()

    def _drain_queue_abort(self, exc: Exception) -> None:
        """После фатальной ошибки будит всех, кто ждёт sync в очереди."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            if item[0] == "sync":
                result, done = item[4], item[5]
                result["error"] = exc
                done.set()

    @staticmethod
    def _flush_guarded(
        con: duckdb.DuckDBPyConnection,
        rows: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """_flush с логированием размера потерянного пакета — и ЖЁСТКИМ пробросом.

        Дефект А: _flush_guarded раньше глотал исключение (_flush делает
        ROLLBACK при ConstraintException/InvalidInputException), и весь пакет
        терялся молча. Теперь любая ошибка пачки логируется (размер пакета) и
        пробрасывается наружу — потеря пакета обязана быть слышимой.
        """
        try:
            StoreWriter._flush(con, rows)
        except Exception:
            log.exception(
                "store-writer: ПОТЕРЯН пакет из %d строк — проброс наверх",
                len(rows),
            )
            raise

    @staticmethod
    def _flush(
        con: duckdb.DuckDBPyConnection,
        rows: list[tuple[str, dict[str, Any]]],
    ) -> None:
        if not rows:
            return
        by_table: dict[str, list[dict[str, Any]]] = {}
        for table, row in rows:
            by_table.setdefault(table, []).append(row)
        con.execute("BEGIN TRANSACTION")
        try:
            for table, group in by_table.items():
                cols: list[str] = []
                seen: set[str] = set()
                for r in group:
                    for c in r:
                        if c not in seen:
                            seen.add(c)
                            cols.append(c)
                if not cols:
                    continue
                group = _dedupe_by_key(table, group)
                if not group:
                    continue
                col_sql = ", ".join(cols)
                _write_group_arrow(con, table, cols, group)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def _dedupe_by_key(
    table: str, group: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Оставляет первую строку по естественному ключу (INSERT OR IGNORE).

    Один INSERT ... SELECT из Arrow-таблицы не умеет игнорировать дубликаты
    ВНУТРИ пачки (DuckDB бросает ConstraintException). executemany с
    построчным INSERT OR IGNORE такие дубликаты пропускал. Пример: у
    recon_checks ключ (token_id, seq) — локальный seq уникален в рамках
    токена, поэтому ни одно сравнение не теряется даже при совпадении
    ts_recv_ms у двух book-событий (ЗАДАЧА 3: "throw away nothing").
    """
    key_cols = schema.TABLES[table][1]
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for r in group:
        k = tuple(r.get(c) for c in key_cols)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _write_group_arrow(
    con: duckdb.DuckDBPyConnection,
    table: str,
    cols: list[str],
    group: list[dict[str, Any]],
) -> None:
    """INSERT OR IGNORE пачки через Arrow вместо duckdb.executemany.

    Причина: duckdb.executemany со списком списков параметров при
    отсутствии установленного pandas бесконечно повторяет `import pandas`
    внутри C-кода и зависает (воспроизведено в 2026-08-02). Вставка через
    зарегистрированную Arrow-таблицу этот путь не трогает и на 100k строк
    занимает ~0.3s.

    Регистрация идёт под фиксированным именем (_TMP_TABLE): соединение
    принадлежит единственному потоку-писателю, конкурентной регистрации
    с тем же именем быть не может.
    """
    table_spec = schema.TABLES[table]
    col_types = table_spec[0]
    arrays = []
    for col in cols:
        values = [r.get(col) for r in group]
        pa_type = _pa_type_for(col_types[col]) if col in col_types else None
        if pa_type is not None:
            arrays.append(pa.array(values, type=pa_type))
        else:
            arrays.append(pa.array(values))
    tbl = pa.Table.from_arrays(arrays, names=cols)
    col_sql = ", ".join(cols)
    con.register(_TMP_TABLE, tbl)
    try:
        con.execute(
            f"INSERT OR IGNORE INTO {table} ({col_sql}) SELECT * FROM {_TMP_TABLE}"
        )
    finally:
        con.unregister(_TMP_TABLE)


class SyncWriter:
    """Прямая синхронная запись (тесты и одиночные запуски).

    Тот же интерфейс, что у StoreWriter, но без потока: каждая операция
    выполняется немедленно на соединении вызывающего потока.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def submit_row(self, table: str, row: dict[str, Any]) -> None:
        insert_row(self._con, table, row)

    def submit_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        fn(self._con, *args, **kwargs)

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return fn(self._con, *args, **kwargs)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


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


def current_seqs(
    con: duckdb.DuckDBPyConnection,
    table: str,
    token_ids: Iterable[str],
) -> dict[str, int]:
    """{token_id: следующий seq} одним запросом (для prewarm до цикла приёма)."""
    ids = [str(t) for t in token_ids]
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT token_id, COALESCE(MAX(seq), 0) + 1 "
        f"FROM {table} WHERE token_id IN ({placeholders}) GROUP BY token_id",
        ids,
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


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
