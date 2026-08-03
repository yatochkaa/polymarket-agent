"""Зафиксированные схемы коллектора (контракт из SCHEMAS.md).

Единственное место, где объявлены имена/типы колонок. duckdb-DDL строится
из этих списков, parquet-проекция для сверки — тоже отсюда.

Запрещено менять имена/типы существующих колонок без повторного probe:
см. SCHEMAS.md, раздел «ЗАМОРОЖЕННАЯ СХЕМА КОЛЛЕКТОРА».
"""

from __future__ import annotations

import pyarrow as pa

# Проекция book_snapshots для сверки с pmdata (замороженная схема book_poller):
# ровно те же 12 колонок и типы, что в src/validate/book_poller.BOOK_SCHEMA.
BOOK_EXPORT_SCHEMA = pa.schema(
    [
        pa.field("ts_recv_ms", pa.int64()),
        pa.field("ts_server_ms", pa.int64(), nullable=True),
        pa.field("token_id", pa.string()),
        pa.field("best_bid", pa.float64(), nullable=True),
        pa.field("best_ask", pa.float64(), nullable=True),
        pa.field("bid_size", pa.float64(), nullable=True),
        pa.field("ask_size", pa.float64(), nullable=True),
        pa.field("spread", pa.float64(), nullable=True),
        pa.field("vwap_bid_100", pa.float64(), nullable=True),
        pa.field("vwap_ask_100", pa.float64(), nullable=True),
        pa.field("book_age_ms", pa.int64(), nullable=True),
        pa.field("seq", pa.int64()),
    ]
)

BOOK_SNAPSHOTS_COLUMNS = {
    "ts_recv_ms": "BIGINT NOT NULL",
    "ts_server_ms": "BIGINT",
    "token_id": "TEXT NOT NULL",
    "best_bid": "DOUBLE",
    "best_ask": "DOUBLE",
    "bid_size": "DOUBLE",
    "ask_size": "DOUBLE",
    "spread": "DOUBLE",
    "vwap_bid_100": "DOUBLE",
    "vwap_ask_100": "DOUBLE",
    "book_age_ms": "BIGINT",
    "seq": "BIGINT NOT NULL",
    "source": "TEXT NOT NULL",
}
BOOK_SNAPSHOTS_KEY = ("token_id", "seq")

TICK_CHANGES_COLUMNS = {
    "ts_recv_ms": "BIGINT NOT NULL",
    "ts_server_ms": "BIGINT",
    "token_id": "TEXT NOT NULL",
    "event_type": "TEXT NOT NULL",
    "side": "TEXT",
    "price": "DOUBLE",
    "size": "DOUBLE",
    "best_bid": "DOUBLE",
    "best_ask": "DOUBLE",
    "raw": "TEXT NOT NULL",
    "seq": "BIGINT NOT NULL",
}
TICK_CHANGES_KEY = ("token_id", "seq")

GAP_INTERVALS_COLUMNS = {
    "token_id": "TEXT NOT NULL",
    "start_ms": "BIGINT NOT NULL",
    "end_ms": "BIGINT NOT NULL",
    "reason": "TEXT NOT NULL",
    "n_missing": "BIGINT",
}
GAP_INTERVALS_KEY = ("token_id", "start_ms", "end_ms", "reason")

RECON_CHECKS_COLUMNS = {
    "ts_recv_ms": "BIGINT NOT NULL",
    "token_id": "TEXT NOT NULL",
    "seq": "BIGINT NOT NULL",
    "n_levels_ours": "BIGINT NOT NULL",
    "n_levels_theirs": "BIGINT NOT NULL",
    "max_abs_diff_price": "DOUBLE NOT NULL",
    "max_abs_diff_size": "DOUBLE NOT NULL",
    "verdict": "TEXT NOT NULL",
}
# Ключ включает локальный seq: два book-события одного токена в одну
# миллисекунду (ts_recv_ms совпал) НЕ должны затирать друг друга — recon
# обязан сохранить каждое сравнение (ЗАДАЧА 3: "throw away nothing").
RECON_CHECKS_KEY = ("token_id", "seq")

COLLECTOR_SESSIONS_COLUMNS = {
    "session_id": "TEXT NOT NULL",
    "started_ms": "BIGINT NOT NULL",
    "ended_ms": "BIGINT",
    "git_commit": "TEXT",
    "markets_subscribed": "INTEGER NOT NULL",
    "exit_reason": "TEXT",
}
COLLECTOR_SESSIONS_KEY = ("session_id",)

# Per-connection статистика сессии (приёмка мультисоединённого транспорта,
# Задача 2, решение владельца 2026-08-03). Пишет коллектор в конце run();
# читает probes/deepseek/probe_accept_conns.py. Одну строку на соединение.
# first_msg_ms/last_msg_ms могут быть NULL: соединение могло не получить
# ни одного сообщения (сразу встало) — это ДАННЫЕ для C4, а не отсутствие.
CONN_STATS_COLUMNS = {
    "session_id": "TEXT NOT NULL",
    "conn_id": "INTEGER NOT NULL",
    "n_tokens": "INTEGER NOT NULL",
    "messages": "BIGINT NOT NULL",
    "events": "BIGINT NOT NULL",
    "recons": "BIGINT NOT NULL",
    "recons_mismatch": "BIGINT NOT NULL",
    "reconnects": "BIGINT NOT NULL",
    "max_silence_s": "DOUBLE NOT NULL",
    "n_silence_episodes": "INTEGER NOT NULL",
    "n_pings_fired": "INTEGER NOT NULL",
    "first_msg_ms": "BIGINT",
    "last_msg_ms": "BIGINT",
}
CONN_STATS_KEY = ("session_id", "conn_id")

MARKETS_TRACKED_COLUMNS = {
    "token_id": "TEXT NOT NULL",
    "market_id": "TEXT",
    "event_id": "TEXT",
    "vertical": "TEXT",
    "start_ms": "BIGINT NOT NULL",
    "end_ms": "BIGINT",
    "resolved": "BOOLEAN",
}
MARKETS_TRACKED_KEY = ("token_id",)

OWN_ORDERS_COLUMNS = {
    "id": "TEXT NOT NULL",
}
OWN_ORDERS_KEY = ("id",)

TABLES: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {
    "book_snapshots": (BOOK_SNAPSHOTS_COLUMNS, BOOK_SNAPSHOTS_KEY),
    "tick_changes": (TICK_CHANGES_COLUMNS, TICK_CHANGES_KEY),
    "gap_intervals": (GAP_INTERVALS_COLUMNS, GAP_INTERVALS_KEY),
    "recon_checks": (RECON_CHECKS_COLUMNS, RECON_CHECKS_KEY),
    "collector_sessions": (COLLECTOR_SESSIONS_COLUMNS, COLLECTOR_SESSIONS_KEY),
    "conn_stats": (CONN_STATS_COLUMNS, CONN_STATS_KEY),
    "markets_tracked": (MARKETS_TRACKED_COLUMNS, MARKETS_TRACKED_KEY),
    "own_orders": (OWN_ORDERS_COLUMNS, OWN_ORDERS_KEY),
}

GAP_REASONS = frozenset({"time_gap", "server_resync", "disconnect", "process_restart"})
RECON_VERDICTS = frozenset({"warmup", "match", "mismatch"})
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
SOURCE_WS = "ws"
SOURCE_REST_BACKFILL = "rest_backfill"
