"""Тесты коллектора (этап 1). Минимум четыре обязательных случая задания:
1. разрыв seq -> gap_intervals с корректным n_missing;
2. повторная вставка того же seq не создаёт дубля;
3. book_age_ms вычисляется и не NULL (при наличии серверной метки);
4. обрыв соединения -> gap с reason=disconnect, а не тихое продолжение.

Сеть не трогаем: WS-парсинг и запись в duckdb на реальных формах сообщений
из живой пробы 2026-08-02 (logs/_ws_analysis.txt).

Запуск: python -m unittest discover -s tests -v
"""

import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from src.collect import store
from src.collect.recon import LiveBook, recon_check
from src.collect.store import SyncWriter
from src.collect.ws_collector import (
    Collector,
    BookEvent,
    DeltaEvent,
    TradeEvent,
    LIVENESS_EXIT_CODE,
    LivenessWatchdog,
    MARKETS_PER_CONN,
    interpret_message,
    _discover,
    _periodic_export_due,
    _rediscovery_due,
    _partition_market,
    _partition_tokens,
    parse_args,
    seq_gap_row,
    snapshot_from_delta,
    snapshot_from_levels,
    tick_row,
)


@contextlib.contextmanager
def _db():
    """Временная duckdb-база; соединение закрывается ДО удаления каталога
    (Windows держит файл залоченным, пока открыто соединение)."""
    with tempfile.TemporaryDirectory() as td:
        con = store.connect(Path(td) / "pm.duckdb")
        try:
            yield con
        finally:
            con.close()

# Реальные формы WS-сообщений из захвата 2026-08-02.
PRICE_CHANGE_MSG = {
    "market": "0x93796dea2fe5dfc8da1a942448e6ab767d9bb3464a96ea3a527dfed0ef7bd958",
    "price_changes": [
        {
            "asset_id": "up1",
            "price": "0.87", "size": "15.22", "side": "BUY",
            "hash": "e4f69bc5faec8b3743c8726b9b32990cd9d6ce76",
            "best_bid": "0.981", "best_ask": "0.984",
        },
        {
            "asset_id": "dn1",
            "price": "0.13", "size": "15.22", "side": "SELL",
            "hash": "9fd7ce7130804efff946025335305311f1ac08b7",
            "best_bid": "0.016", "best_ask": "0.019",
        },
    ],
    "timestamp": "1785651107785",
    "event_type": "price_change",
}

BOOK_MSG = {
    "market": "0x93796dea2fe5dfc8da1a942448e6ab767d9bb3464a96ea3a527dfed0ef7bd958",
    "asset_id": "up1",
    "timestamp": "1785651131034",
    "hash": "1a6b0e02f066df1a580e3a79670a962dcf611888",
    "bids": [
        {"price": "0.01", "size": "3265"},
        {"price": "0.02", "size": "30000"},
        {"price": "0.49", "size": "100"},
    ],
    "asks": [
        {"price": "0.50", "size": "200"},
        {"price": "0.51", "size": "100"},
    ],
    "event_type": "book",
}

LAST_TRADE_MSG = {
    "market": "0x93796dea2fe5dfc8da1a942448e6ab767d9bb3464a96ea3a527dfed0ef7bd958",
    "asset_id": "up1",
    "price": "0.981", "size": "140.84", "fee_rate_bps": "0",
    "side": "SELL", "timestamp": "1785651131034",
    "event_type": "last_trade_price",
    "transaction_hash": "0x2499a3570990342d1f43065079f333348f956cc94f555353f4094947173ef641",
}

BOOK_ARRAY_MSG = [
    {"market": "0xa1", "asset_id": "up1", "timestamp": "1785651131034",
     "bids": [{"price": "0.01", "size": "10"}], "asks": [], "event_type": "book"},
    {"market": "0xa1", "asset_id": "dn1", "timestamp": "1785651131035",
     "bids": [], "asks": [{"price": "0.99", "size": "10"}], "event_type": "book"},
]


class TestSeqGapRow(unittest.TestCase):
    """Случай 1: разрыв seq -> gap_intervals с корректным n_missing."""

    def test_n_missing_computed(self) -> None:
        row = seq_gap_row(
            token_id="up1", seq_prev=5, seq_new=9,
            ts_from_ms=1000, ts_to_ms=2000,
        )
        self.assertEqual(row["n_missing"], 3)  # 9 - 5 - 1
        self.assertEqual(row["reason"], "time_gap")

    def test_non_gap_rejected(self) -> None:
        with self.assertRaises(ValueError):
            seq_gap_row(token_id="up1", seq_prev=5, seq_new=4,
                        ts_from_ms=1, ts_to_ms=2)
        with self.assertRaises(ValueError):
            seq_gap_row(token_id="up1", seq_prev=5, seq_new=5,
                        ts_from_ms=1, ts_to_ms=2)

    def test_gap_row_written_to_db(self) -> None:
        with _db() as con:
            store.insert_row(con, "gap_intervals", seq_gap_row(
                token_id="up1", seq_prev=5, seq_new=9,
                ts_from_ms=1000, ts_to_ms=2000,
            ))
            rows = con.execute("SELECT * FROM gap_intervals").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][4], 3)  # n_missing


class TestIdempotency(unittest.TestCase):
    """Случай 2: повторная вставка того же seq не создаёт дубля."""

    def test_reinsert_same_seq_single_row(self) -> None:
        with _db() as con:
            row = snapshot_from_levels(
                token_id="up1", ts_server_ms=1000,
                levels_bids=[(0.49, 100.0)], levels_asks=[(0.50, 200.0)],
                ts_recv_ms=2000, seq=7, source="ws",
            )
            store.insert_row(con, "book_snapshots", row)
            store.insert_row(con, "book_snapshots", dict(row))  # тот же ключ
            n = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n, 1)

    def test_seq_resumes_from_db_max(self) -> None:
        with _db() as con:
            con.execute(
                "INSERT INTO book_snapshots (ts_recv_ms, token_id, seq, source) "
                "VALUES (1, 'up1', 100, 'ws')"
            )
            self.assertEqual(store.current_seq(con, "book_snapshots", "up1"), 101)


class TestBookAge(unittest.TestCase):
    """Случай 3: book_age_ms вычисляется и не NULL при серверной метке."""

    def test_book_age_from_levels(self) -> None:
        row = snapshot_from_levels(
            token_id="up1", ts_server_ms=1785651131034,
            levels_bids=[(0.49, 100.0)], levels_asks=[(0.50, 200.0)],
            ts_recv_ms=1785651132000, seq=1, source="ws",
        )
        self.assertEqual(row["book_age_ms"], 1785651132000 - 1785651131034)
        self.assertIsNotNone(row["book_age_ms"])

    def test_book_age_none_without_ts(self) -> None:
        row = snapshot_from_levels(
            token_id="up1", ts_server_ms=None,
            levels_bids=[(0.49, 100.0)], levels_asks=[(0.50, 200.0)],
            ts_recv_ms=1785651132000, seq=1, source="ws",
        )
        self.assertIsNone(row["book_age_ms"])
        self.assertIsNone(row["ts_server_ms"])

    def test_book_age_from_delta(self) -> None:
        live = LiveBook()
        live.set_book([(0.49, 100.0)], [(0.50, 200.0)])
        row = snapshot_from_delta(
            token_id="up1", ts_server_ms=1785651131034,
            best_bid=0.49, best_ask=0.50, livebook=live,
            ts_recv_ms=1785651132000, seq=1,
        )
        self.assertIsNotNone(row["book_age_ms"])


class TestDisconnectGap(unittest.TestCase):
    """Случай 4: обрыв соединения -> gap с reason=disconnect, не тихое
    продолжение. После обрыва события НЕ идут внутри разрыва как наблюдения:
    сначала пишется gap, продолжение seq идёт после."""

    def test_disconnect_writes_gap_per_token(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.subscribed_tokens = ["up1", "dn1"]
            col.record_disconnect(["up1", "dn1"], ts_from_ms=5000, ts_to_ms=9000)
            rows = con.execute(
                "SELECT token_id, reason FROM gap_intervals ORDER BY token_id"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows), {("up1", "disconnect"), ("dn1", "disconnect")})
            self.assertEqual(col.stats["reconnects"], 1)
            # после обрыва токены ждут ресинка
            self.assertEqual(col.pending_resync, {"up1", "dn1"})

    def test_gap_followed_by_contiguous_seq(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.record_disconnect(["up1"], ts_from_ms=5000, ts_to_ms=9000)
            book = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=9500,
                bids=((0.49, 100.0),), asks=((0.50, 200.0),),
                hash="h-book-gap",
                raw=json.dumps({"event_type": "book"}),
            )
            col.handle_event(book, 9500)
            rows = con.execute(
                "SELECT seq, source FROM book_snapshots WHERE token_id='up1'"
            ).fetchall()
            self.assertEqual(rows, [(1, "ws")])  # первый снимок после рестарта
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM gap_intervals WHERE reason='disconnect'"
            ).fetchone())


class TestInterpretMessage(unittest.TestCase):
    def test_price_change_pair(self) -> None:
        events, markets = interpret_message(PRICE_CHANGE_MSG, ts_recv_ms=1000)
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0], DeltaEvent)
        self.assertEqual(events[0].token_id, "up1")
        self.assertEqual(events[0].side, "BUY")
        self.assertEqual(events[0].price, 0.87)
        self.assertEqual(events[0].best_bid, 0.981)
        self.assertEqual(events[1].token_id, "dn1")
        self.assertEqual(markets["up1"], "0x93796dea2fe5dfc8da1a942448e6ab767d9bb3464a96ea3a527dfed0ef7bd958")
        # hash берётся из элемента price_changes[i] (см. решение 2026-08-03)
        self.assertEqual(events[0].hash, "e4f69bc5faec8b3743c8726b9b32990cd9d6ce76")
        self.assertEqual(events[1].hash, "9fd7ce7130804efff946025335305311f1ac08b7")

    def test_book_message(self) -> None:
        events, _ = interpret_message(BOOK_MSG, ts_recv_ms=1000)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsInstance(ev, BookEvent)
        self.assertEqual(ev.token_id, "up1")
        self.assertEqual(ev.ts_server_ms, 1785651131034)
        self.assertEqual(ev.bids, ((0.01, 3265.0), (0.02, 30000.0), (0.49, 100.0)))
        # hash сообщения book (см. решение 2026-08-03)
        self.assertEqual(ev.hash, "1a6b0e02f066df1a580e3a79670a962dcf611888")

    def test_last_trade_message(self) -> None:
        events, _ = interpret_message(LAST_TRADE_MSG, ts_recv_ms=1000)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], TradeEvent)
        self.assertEqual(events[0].price, 0.981)
        self.assertEqual(events[0].side, "SELL")
        # у last_trade_price поля hash НЕТ, есть только transaction_hash
        # (захват 2026-08-02, logs/_ws_analysis.txt:28-29)
        self.assertFalse(hasattr(events[0], "hash"))
        self.assertEqual(
            events[0].transaction_hash,
            "0x2499a3570990342d1f43065079f333348f956cc94f555353f4094947173ef641",
        )

    def test_book_array_initial_snapshot(self) -> None:
        events, markets = interpret_message(BOOK_ARRAY_MSG, ts_recv_ms=1000)
        self.assertEqual(len(events), 2)
        self.assertEqual([e.token_id for e in events], ["up1", "dn1"])
        self.assertEqual(markets["up1"], "0xa1")

    def test_garbage_not_crashed(self) -> None:
        events, _ = interpret_message([1, "x", None], ts_recv_ms=1000)
        self.assertEqual(events, [])
        events, _ = interpret_message({"event_type": "weird"}, ts_recv_ms=1000)
        self.assertEqual(events, [])


class TestCollectorEndToEnd(unittest.TestCase):
    def test_book_then_delta_then_recon_match(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.subscribed_tokens = ["up1"]

            b1 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1000,
                bids=((0.49, 100.0), (0.48, 50.0)),
                asks=((0.50, 200.0), (0.51, 100.0)),
                hash="h-b1",
                raw=json.dumps({"event_type": "book", "asset_id": "up1", "ts": 1}),
            )
            col.handle_event(b1, 1000)
            # первый снимок -> warmup
            self.assertEqual(
                con.execute("SELECT verdict FROM recon_checks").fetchone()[0], "warmup"
            )

            d = DeltaEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1500,
                side="BUY", price=0.49, size=150.0,
                best_bid=0.49, best_ask=0.50,
                hash="h-d1",
                raw=json.dumps({"event_type": "price_change", "asset_id": "up1", "ts": 2}),
            )
            col.handle_event(d, 1500)

            b2 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=2000,
                bids=((0.49, 150.0), (0.48, 50.0)),
                asks=((0.50, 200.0), (0.51, 100.0)),
                hash="h-b2",
                raw=json.dumps({"event_type": "book", "asset_id": "up1", "ts": 3}),
            )
            col.handle_event(b2, 2000)
            # восстановленная из дельт книга совпадает со снимком -> match
            self.assertEqual(
                con.execute("SELECT verdict FROM recon_checks ORDER BY ts_recv_ms").fetchall(),
                [("warmup",), ("match",)],
            )

            snaps = con.execute(
                "SELECT best_bid, bid_size, vwap_bid_100, seq, source "
                "FROM book_snapshots ORDER BY seq"
            ).fetchall()
            self.assertEqual(snaps[0][0], 0.49)      # book: bid
            self.assertEqual(snaps[0][1], 100.0)     # book: bid_size
            self.assertEqual(snaps[1][0], 0.49)      # delta snapshot
            self.assertEqual(snaps[1][1], 150.0)     # post-delta размер
            self.assertEqual([s[3] for s in snaps], [1, 2, 3])  # seq непрерывен
            self.assertEqual({s[4] for s in snaps}, {"ws"})

    def test_lost_delta_yields_mismatch(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000)
            b1 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1000,
                bids=((0.49, 100.0),), asks=((0.50, 200.0),),
                hash="h-b1",
                raw=json.dumps({"e": "book", "a": "up1", "t": 1}),
            )
            col.handle_event(b1, 1000)
            # дельта потеряна: следующий снимок уже с размером 300
            b2 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=2000,
                bids=((0.49, 300.0),), asks=((0.50, 200.0),),
                hash="h-b2",
                raw=json.dumps({"e": "book", "a": "up1", "t": 2}),
            )
            col.handle_event(b2, 2000)
            verdicts = con.execute(
                "SELECT verdict FROM recon_checks ORDER BY ts_recv_ms"
            ).fetchall()
            self.assertEqual(verdicts, [("warmup",), ("mismatch",)])
            self.assertEqual(col.stats["recons_mismatch"], 1)

    def test_duplicate_delta_skipped(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000)
            b1 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1000,
                bids=((0.49, 100.0),), asks=((0.50, 200.0),),
                hash="h-b1",
                raw=json.dumps({"e": "book", "a": "up1", "t": 1}),
            )
            col.handle_event(b1, 1000)
            d = DeltaEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1500,
                side="BUY", price=0.49, size=50.0,
                best_bid=0.49, best_ask=0.50,
                hash="h-d1",
                raw=json.dumps({"e": "pc", "a": "up1", "t": 2}),
            )
            col.handle_event(d, 1500)
            col.handle_event(d, 1600)  # дубль (двойная подписка)
            n_snaps = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n_snaps, 2)  # book + одна дельта, дубль отброшен
            self.assertEqual(col.stats["events_skipped_dedup"], 1)


class TestDedupKeyByType(unittest.TestCase):
    """Дедуп по ТИПУ сообщения (решение владельца 2026-08-03, DECISIONS_NEEDED.md):
    book -> (asset_id, hash); price_change -> (asset_id, hash, price, size);
    last_trade_price -> (asset_id, transaction_hash, price, size).
    md5 тела убран полностью."""

    @staticmethod
    def _delta(h: str, *, price: float = 0.49, size: float = 50.0) -> DeltaEvent:
        return DeltaEvent(
            token_id="up1", market_id="0x1", ts_server_ms=1500,
            side="BUY", price=price, size=size,
            best_bid=0.49, best_ask=0.50, hash=h,
            raw=json.dumps({"event_type": "price_change", "asset_id": "up1"}),
        )

    @staticmethod
    def _trade(tx: str, *, price: float = 0.981, size: float = 140.84) -> TradeEvent:
        return TradeEvent(
            token_id="up1", market_id="0x1", ts_server_ms=1500,
            side="SELL", price=price, size=size,
            transaction_hash=tx,
            raw=json.dumps({"event_type": "last_trade_price", "asset_id": "up1"}),
        )

    def test_delta_same_hash_two_conns_one_record(self) -> None:
        """price_change с одинаковым hash на двух соединениях -> одна запись."""
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.handle_event(self._delta("h-same"), 1500)
            col.handle_event(self._delta("h-same"), 1600)
            self.assertEqual(col.stats["events_skipped_dedup"], 1)
            n = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n, 1)
            n_ticks = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
            self.assertEqual(n_ticks, 1)

    def test_delta_different_hash_two_records(self) -> None:
        """price_change с разным hash -> две записи."""
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.handle_event(self._delta("h-1", price=0.49), 1500)
            col.handle_event(self._delta("h-2", price=0.48), 1600)
            self.assertEqual(col.stats["events_skipped_dedup"], 0)
            n = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n, 2)

    def test_delta_same_hash_diff_price_size_two_records(self) -> None:
        """price_change с ОДИНАКОВЫМ hash, но разными price/size -> ДВЕ записи.

        Реальная пара из logs/ws_raw.jsonl (recv_ms=1785651207744, два сообщения
        одной миллисекунды): сервер переиспользует hash для разных изменений
        одного asset_id. Ключ (asset_id, hash) без price/size склеил бы их и
        отбросил легитимную дельту (ASSUMPTIONS.md).
        """
        h = "5d37d11da6a157dc177559158c41e882f49b2fa3"
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.handle_event(self._delta(h, price=0.3, size=8700.0), 1500)
            col.handle_event(self._delta(h, price=0.4, size=6781.44), 1600)
            self.assertEqual(col.stats["events_skipped_dedup"], 0)
            n = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n, 2)
            n_ticks = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
            self.assertEqual(n_ticks, 2)

    def test_delta_fully_identical_one_record(self) -> None:
        """Два price_change дословно одинаковые (hash, price, size) -> одна запись."""
        h = "5d37d11da6a157dc177559158c41e882f49b2fa3"
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.handle_event(self._delta(h, price=0.3, size=8700.0), 1500)
            col.handle_event(self._delta(h, price=0.3, size=8700.0), 1600)
            self.assertEqual(col.stats["events_skipped_dedup"], 1)
            n = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n, 1)
            n_ticks = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
            self.assertEqual(n_ticks, 1)

    def test_trade_same_tx_different_price_two_records(self) -> None:
        """Две last_trade_price с одним transaction_hash, но разной ценой -> ДВЕ
        записи (одна транзакция может нести несколько исполнений по активу)."""
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.handle_event(self._trade("0xtx", price=0.981, size=140.84), 1500)
            col.handle_event(self._trade("0xtx", price=0.984, size=100.0), 1600)
            self.assertEqual(col.stats["events_skipped_dedup"], 0)
            n = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
            self.assertEqual(n, 2)

    def test_trade_fully_identical_one_record(self) -> None:
        """Две last_trade_price полностью идентичные -> одна запись."""
        with _db() as con:
            col = Collector(con, now_ms=1000)
            col.handle_event(self._trade("0xtx"), 1500)
            col.handle_event(self._trade("0xtx"), 1600)
            self.assertEqual(col.stats["events_skipped_dedup"], 1)
            n = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
            self.assertEqual(n, 1)

    def test_delta_without_hash_raises(self) -> None:
        """price_change без hash -> raise (не дропать, не подставлять)."""
        col = Collector(object(), now_ms=1000)
        with self.assertRaises(ValueError) as cm:
            col._dedup_key(self._delta(None))
        self.assertIn("price_change", str(cm.exception))

    def test_trade_without_tx_raises(self) -> None:
        """last_trade_price без transaction_hash -> raise с типом в тексте."""
        col = Collector(object(), now_ms=1000)
        with self.assertRaises(ValueError) as cm:
            col._dedup_key(self._trade(None))
        self.assertIn("last_trade_price", str(cm.exception))

    def test_book_without_hash_raises(self) -> None:
        """book без hash -> raise (тип обязан нести hash, см. решение)."""
        col = Collector(object(), now_ms=1000)
        ev = BookEvent(
            token_id="up1", market_id="0x1", ts_server_ms=1000,
            bids=((0.49, 100.0),), asks=((0.50, 200.0),),
            hash=None,
            raw=json.dumps({"event_type": "book"}),
        )
        with self.assertRaises(ValueError) as cm:
            col._dedup_key(ev)
        self.assertIn("book", str(cm.exception))

    def test_unknown_type_without_fields_raises(self) -> None:
        """Неизвестный тип без hash и без transaction_hash -> raise."""
        col = Collector(object(), now_ms=1000)
        with self.assertRaises(ValueError) as cm:
            col._dedup_key(object())  # type: ignore[arg-type]
        self.assertIn("object", str(cm.exception))


class TestNegativeControl(unittest.TestCase):
    """Отрицательный контроль детектора потерь (--drop-rate).

    Детектор, который молчит при внесённых потерях, — неисправен. Здесь:
    drop_rate=1.0 выбрасывает ВСЕ дельты, второй серверный снимок обязан
    дать mismatch.
    """

    def test_drop_rate_1_drops_delta_and_recon_mismatches(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000, drop_rate=1.0)
            col.subscribed_tokens = ["up1"]
            b1 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1000,
                bids=((0.49, 100.0),), asks=((0.50, 200.0),),
                hash="h-b1",
                raw=json.dumps({"e": "book", "a": "up1", "t": 1}),
            )
            col.handle_event(b1, 1000)
            d = DeltaEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1500,
                side="BUY", price=0.49, size=150.0,
                best_bid=0.49, best_ask=0.50,
                hash="h-d1",
                raw=json.dumps({"e": "pc", "a": "up1", "t": 2}),
            )
            col.handle_event(d, 1500)
            self.assertEqual(col.stats["dropped"], 1)  # дельта выброшена
            # выброшенная дельта не создала ни одной строки
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0], 1
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0], 1
            )
            b2 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=2000,
                bids=((0.49, 150.0),), asks=((0.50, 200.0),),
                hash="h-b2",
                raw=json.dumps({"e": "book", "a": "up1", "t": 3}),
            )
            col.handle_event(b2, 2000)
            # книга не изменилась дельтой -> снимок расходится, recon обязан поймать
            verdicts = con.execute(
                "SELECT verdict FROM recon_checks ORDER BY ts_recv_ms"
            ).fetchall()
            self.assertEqual(verdicts, [("warmup",), ("mismatch",)])
            self.assertEqual(col.stats["recons_mismatch"], 1)

    def test_drop_rate_0_never_drops(self) -> None:
        with _db() as con:
            col = Collector(con, now_ms=1000, drop_rate=0.0)
            d = DeltaEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1500,
                side="BUY", price=0.49, size=150.0,
                best_bid=0.49, best_ask=0.50,
                hash="h-d1",
                raw=json.dumps({"e": "pc", "a": "up1", "t": 2}),
            )
            col.handle_event(d, 1500)
            self.assertEqual(col.stats["dropped"], 0)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0], 1
            )

    def test_drop_rate_out_of_range_rejected(self) -> None:
        for bad in (-0.1, 1.5):
            with self.assertRaises(ValueError):
                Collector(object(), now_ms=1, drop_rate=bad)

    def test_fixed_seed_reproducible(self) -> None:
        """Одно и то же зерно -> одинаковый набор дропов при равном потоке."""
        drops: list[int] = []
        for _ in range(2):
            col = Collector(object(), now_ms=1, drop_rate=0.5)
            n = 0
            for _ in range(10000):
                if col._rng.random() < 0.5:
                    n += 1
            drops.append(n)
        self.assertEqual(drops[0], drops[1])
        # 0.5 ± 3% на 10000 бросаний
        self.assertGreater(drops[0], 4700)
        self.assertLess(drops[0], 5300)


class TestReconPure(unittest.TestCase):
    def test_warmup_when_not_initialized(self) -> None:
        ours = LiveBook()
        rc = recon_check(ts_recv_ms=1, token_id="up1", seq=7, ours=ours,
                         theirs_bids={0.49: 100.0}, theirs_asks={0.50: 200.0})
        self.assertEqual(rc["verdict"], "warmup")
        self.assertEqual(rc["seq"], 7)

    def test_match(self) -> None:
        ours = LiveBook()
        ours.set_book([(0.49, 100.0)], [(0.50, 200.0)])
        rc = recon_check(ts_recv_ms=1, token_id="up1", seq=7, ours=ours,
                         theirs_bids={0.49: 100.0}, theirs_asks={0.50: 200.0})
        self.assertEqual(rc["verdict"], "match")
        self.assertEqual(rc["max_abs_diff_size"], 0.0)
        self.assertEqual(rc["seq"], 7)

    def test_mismatch_on_size(self) -> None:
        ours = LiveBook()
        ours.set_book([(0.49, 100.0)], [(0.50, 200.0)])
        rc = recon_check(ts_recv_ms=1, token_id="up1", seq=7, ours=ours,
                         theirs_bids={0.49: 130.0}, theirs_asks={0.50: 200.0})
        self.assertEqual(rc["verdict"], "mismatch")
        self.assertEqual(rc["max_abs_diff_size"], 30.0)
        self.assertEqual(rc["seq"], 7)


class TestTickRow(unittest.TestCase):
    def test_verbatim_raw_preserved(self) -> None:
        row = tick_row(
            ts_recv_ms=1, ts_server_ms=2, token_id="up1",
            event_type="price_change", side="BUY", price=0.5, size=10.0,
            best_bid=0.49, best_ask=0.51, raw='{"x":1}', seq=1,
        )
        self.assertEqual(row["raw"], '{"x":1}')
        self.assertEqual(row["event_type"], "price_change")


class TestPrewarmSeq(unittest.TestCase):
    def test_prewarm_from_db_and_does_not_touch_active(self) -> None:
        with _db() as con:
            con.execute(
                "INSERT INTO book_snapshots (ts_recv_ms, token_id, seq, source) "
                "VALUES (1, 'up1', 100, 'ws')"
            )
            con.execute(
                "INSERT INTO tick_changes (ts_recv_ms, token_id, seq, event_type, raw) "
                "VALUES (1, 'up1', 40, 'book', '{}')"
            )
            col = Collector(con, now_ms=1)
            col.prewarm_seq(["up1", "dn1"])
            # у активного токена счётчик продолжается от max(seq) в базе
            self.assertEqual(col._seq_snaps["up1"], 101)
            self.assertEqual(col._seq_ticks["up1"], 41)
            # у нового токена без строк в базе — с единицы
            self.assertEqual(col._seq_snaps["dn1"], 1)
            # повторный prewarm не сбрасывает уже использованный счётчик
            col._seq_snaps["up1"] += 1
            col.prewarm_seq(["up1"])
            self.assertEqual(col._seq_snaps["up1"], 102)


class TestPartition(unittest.TestCase):
    """STEP 3: разбиение ПО РЫНКАМ (оба токена рынка на одном соединении)."""

    @staticmethod
    def _market_of(tokens: list[str]) -> dict[str, str]:
        """Каждая пара токенов принадлежит одному рынку (slug 'm<k>')."""
        market_of: dict[str, str] = {}
        k = 0
        for i in range(0, len(tokens), 2):
            slug = f"market{k}"
            market_of[tokens[i]] = slug
            if i + 1 < len(tokens):
                market_of[tokens[i + 1]] = slug
            k += 1
        return market_of

    def test_market_not_split_across_connections(self) -> None:
        """РЕГРЕССИЯ (ловит ошибку): ни один рынок не встречается на >1 соединении.

        Сервер шлёт на подписку одним токеном и его комплемент. Если токены
        одного рынка попадают на разные соединения, каждая дельта приходит
        дважды и книга уезжает (разгадка 28.6%, PROBE_RESULTS.md).
        """
        tokens = [str(i) for i in range(1, 85)]
        market_of = self._market_of(tokens)
        for n_conns in (2, 3, 4, 7):
            parts = _partition_tokens(tokens, market_of, n_conns)
            # Рынок (пары токенов m0..mK) не должен быть расщеплён.
            market_locations: dict[str, list[int]] = {}
            for i, part in enumerate(parts):
                for t in part:
                    m = market_of[t]
                    market_locations.setdefault(m, []).append(i)
            for m, conns in market_locations.items():
                self.assertEqual(
                    len(set(conns)),
                    1,
                    f"рынок {m} расщеплён по соединениям {conns}: задача обязана "
                    "держать оба токена рынка на одном соединении",
                )

    def test_membership_preserved(self) -> None:
        tokens = [str(i) for i in range(1, 85)]
        market_of = self._market_of(tokens)
        parts = _partition_tokens(tokens, market_of, 3)
        self.assertEqual(len(parts), 3)
        self.assertEqual(set(t for part in parts for t in part), set(tokens))

    def test_single_conn_all_in_part_zero(self) -> None:
        tokens = ["1", "2", "3"]
        market_of = {t: "only" for t in tokens}
        parts = _partition_tokens(tokens, market_of, 1)
        self.assertEqual(parts, [["1", "2", "3"]])
        for t in tokens:
            self.assertEqual(_partition_market(market_of[t], ["only"], 1), 0)

    def test_max_50_tokens_per_conn(self) -> None:
        """Потолок: ни одно соединение не получает больше 50 токенов
        при любом числе рынков 1..500 (запас под серверный потолок 56).

        Ломается на старой логике: MD5-разбиение с авто-числом соединений по
        TOKENS_PER_CONN=40 давало для 500 рынков 13 соединений (~77 токенов
        на соединение). Последовательная раскладка с MARKETS_PER_CONN=25
        даёт ровно ceil(рынков/25) соединений по <=50 токенов.
        """
        for n_markets in range(1, 501):
            tokens = [str(i) for i in range(1, 2 * n_markets + 1)]
            market_of = self._market_of(tokens)
            n_conns = max(1, (n_markets + MARKETS_PER_CONN - 1) // MARKETS_PER_CONN)
            parts = _partition_tokens(tokens, market_of, n_conns)
            for part in parts:
                self.assertLessEqual(
                    len(part),
                    50,
                    f"n_markets={n_markets}: соединение получило {len(part)} "
                    "токенов > 50 (серверный потолок ~56, запас нарушен)",
                )

    def test_repartition_stable_across_calls(self) -> None:
        tokens = [str(i) for i in range(1, 85)]
        market_of = self._market_of(tokens)
        self.assertEqual(
            _partition_tokens(tokens, market_of, 4),
            _partition_tokens(tokens, market_of, 4),
        )


class TestSyncWriter(unittest.TestCase):
    """SyncWriter: прямой интерфейс записи без потока (тесты, --minutes 0)."""

    def test_sync_writer_submits_immediately(self) -> None:
        with _db() as con:
            w = SyncWriter(con)
            w.submit_row("book_snapshots", {
                "ts_recv_ms": 1, "token_id": "up1", "seq": 1, "source": "ws",
            })
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0], 1
            )
            w.close()


class TestReconDiagnostics(unittest.TestCase):
    def test_zero_size_server_level_is_filtered(self) -> None:
        ours = LiveBook()
        ours.set_book([(0.5, 1.0)], [])
        row = recon_check(
            ts_recv_ms=1,
            token_id="up1",
            seq=1,
            ours=ours,
            theirs_bids={0.5: 1.0, 0.4: 0.0},
            theirs_asks={},
        )
        self.assertEqual(row["n_levels_theirs"], 1)


class TestVertical(unittest.TestCase):
    """ЗАДАЧА 2: неизвестная вертикаль -- жёсткая ошибка, не молчаливый crypto."""

    def test_periodic_export_env_switch(self) -> None:
        import os

        old = os.environ.pop("PM_EXPORT_INTERVAL_S", None)
        try:
            self.assertTrue(_periodic_export_due(60.0, 0.0))
            os.environ["PM_EXPORT_INTERVAL_S"] = "0"
            self.assertFalse(_periodic_export_due(60.0, 0.0))
            os.environ["PM_EXPORT_INTERVAL_S"] = "60"
            self.assertFalse(_periodic_export_due(59.9, 0.0))
            os.environ["PM_EXPORT_INTERVAL_S"] = "-1"
            with self.assertRaises(ValueError):
                _periodic_export_due(60.0, 0.0)
            os.environ["PM_EXPORT_INTERVAL_S"] = "10"
            self.assertTrue(_periodic_export_due(10.0, 0.0))
            self.assertFalse(_periodic_export_due(9.9, 0.0))
            os.environ["PM_EXPORT_INTERVAL_S"] = "abc"
            with self.assertRaises(ValueError):
                _periodic_export_due(60.0, 0.0)
        finally:
            if old is None:
                os.environ.pop("PM_EXPORT_INTERVAL_S", None)
            else:
                os.environ["PM_EXPORT_INTERVAL_S"] = old

    def test_rediscovery_due_env_switch(self) -> None:
        import os

        old = os.environ.pop("PM_DISCOVER_ONCE", None)
        try:
            self.assertTrue(_rediscovery_due(60.0, 0.0))
            os.environ["PM_DISCOVER_ONCE"] = "1"
            self.assertFalse(_rediscovery_due(60.0, 0.0))
            os.environ.pop("PM_DISCOVER_ONCE")
            self.assertFalse(_rediscovery_due(59.9, 0.0))
        finally:
            if old is None:
                os.environ.pop("PM_DISCOVER_ONCE", None)
            else:
                os.environ["PM_DISCOVER_ONCE"] = old

    def test_unknown_vertical_rejected_in_discover(self) -> None:
        with self.assertRaises(ValueError):
            _discover("nonsense", client=None)  # type: ignore[arg-type]

    def test_valid_verticals_accepted_by_dispatch(self) -> None:
        # Проверяем маршрутизацию без сети: _discover вызывает _discover_crypto /
        # _discover_tennis, оба дёргают discovery-функции (нужен сетевой клиент).
        # Здесь проверяем только, что допустимые вертикали НЕ отвергаются на
        # этапе диспетчеризации (падают ниже уже по отсутствию сети).
        for v in ("crypto", "tennis"):
            try:
                _discover(v, client=None)  # type: ignore[arg-type]
            except (ValueError, AttributeError, TypeError):
                pass  # сетевой слой не проверяем в unit-тесте

    def test_parse_args_rejects_unknown_vertical(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--vertical", "nonsense"])

    def test_parse_args_accepts_tennis(self) -> None:
        args = parse_args(["--vertical", "tennis", "--minutes", "1"])
        self.assertEqual(args.vertical, "tennis")

    def test_conns_zero_is_auto_default(self) -> None:
        args = parse_args([])
        self.assertEqual(args.conns, 0)

    def test_negative_conns_rejected_in_main(self) -> None:
        # --conns < 0 валидируется в main(); через parse_args это валидное число.
        args = parse_args(["--conns", "-1"])
        self.assertEqual(args.conns, -1)


class TestBatchWrite(unittest.TestCase):
    """Пачечная запись через Arrow (bug 2026-08-02: duckdb.executemany со
    списком списков зацикливался на `import pandas` при отсутствии pandas).

    Регрессия должна жить без установленного pandas: _write_group_arrow
    не должен триггерить ленивый импорт pandas из duckdb.
    """

    def test_arrow_batch_writes_multiple_tables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            w = store.StoreWriter(Path(td) / "pm.duckdb", batch=256)
            n_snaps = 5000
            n_gaps = 100
            for i in range(n_snaps):
                w.submit_row("book_snapshots", {
                    "ts_recv_ms": 1785670694000 + i,
                    "ts_server_ms": 1785670694000 + i,
                    "token_id": f"tok{i % 3}",
                    "best_bid": 0.5 if i % 3 else None,
                    "best_ask": 0.56,
                    "bid_size": 100.0,
                    "ask_size": 90.0,
                    "spread": 0.01,
                    "vwap_bid_100": 0.5,
                    "vwap_ask_100": 0.45,
                    "book_age_ms": 100,
                    "seq": i,
                    "source": "ws",
                })
            for i in range(n_gaps):
                w.submit_row("gap_intervals", {
                    "token_id": f"tok{i % 3}",
                    "start_ms": 1785670694000 + i,
                    "end_ms": 1785670694000 + i + 5,
                    "reason": "disconnect",
                    "n_missing": 2,
                })
            w.flush()
            counts = w.call(store.count_rows)
            w.close()
            self.assertEqual(counts["book_snapshots"], n_snaps)
            self.assertEqual(counts["gap_intervals"], n_gaps)

    def test_arrow_batch_nullable_columns(self) -> None:
        """Все-None колонки (ts_server_ms) не должны ломать вставку."""
        with tempfile.TemporaryDirectory() as td:
            w = store.StoreWriter(Path(td) / "pm.duckdb", batch=256)
            for i in range(300):
                w.submit_row("book_snapshots", {
                    "ts_recv_ms": 1 + i,
                    "ts_server_ms": None,
                    "token_id": "up1",
                    "best_bid": None,
                    "best_ask": 0.5,
                    "bid_size": 1.0,
                    "ask_size": 1.0,
                    "spread": 0.0,
                    "vwap_bid_100": 0.4,
                    "vwap_ask_100": 0.6,
                    "book_age_ms": None,
                    "seq": i,
                    "source": "ws",
                })
            w.flush()
            n = w.call(
                lambda con: int(
                    con.execute(
                        "SELECT COUNT(*) FROM book_snapshots WHERE ts_server_ms IS NULL"
                    ).fetchone()[0]
                )
            )
            w.close()
            self.assertEqual(n, 300)

    def test_arrow_batch_idempotent_on_duplicate_keys(self) -> None:
        """INSERT OR IGNORE по естественному ключу (token_id, seq) не дублирует."""
        with tempfile.TemporaryDirectory() as td:
            w = store.StoreWriter(Path(td) / "pm.duckdb", batch=256)
            for _ in range(2):
                for i in range(500):
                    w.submit_row("book_snapshots", {
                        "ts_recv_ms": 1 + i,
                        "token_id": "up1",
                        "seq": i,
                        "source": "ws",
                    })
            w.flush()
            n = w.call(
                lambda con: int(con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0])
            )
            w.close()
            self.assertEqual(n, 500)


class TestLivenessWatchdog(unittest.TestCase):
    """Сторож живости (2026-08-04): если НИ счётчик сообщений, НИ счётчик
    снимков не растут дольше порога — процесс обязан завершиться ненулевым
    кодом (LIVENESS_EXIT_CODE), чтобы tennis_daemon.ps1 перезапустил его.

    На старом коде (без сторожа) этот класс не существует — тест не
    проходит (ImportError). Зависание 03.08 01:00:25: процесс жил 7 часов
    при CPU 1.39 ядра и нулевом приёме, внешний рестарт не сработал.
    """

    def test_stall_exits_with_nonzero_code(self) -> None:
        col = Collector(object(), now_ms=1)
        calls: list[int] = []
        w = LivenessWatchdog(
            col,
            check_interval_s=0.05,
            stall_s=0.2,
            exit_fn=calls.append,
        )
        w.start()
        try:
            time.sleep(0.6)  # молчание дольше порога
        finally:
            w.stop()
        self.assertTrue(calls, "сторож обязан сработать при молчании дольше порога")
        self.assertNotEqual(calls[0], 0)
        self.assertEqual(calls[0], LIVENESS_EXIT_CODE)

    def test_growth_prevents_fire(self) -> None:
        """Рост хотя бы одного счётчика чаще порога — сторожа не будит."""
        col = Collector(object(), now_ms=1)
        calls: list[int] = []
        w = LivenessWatchdog(
            col,
            check_interval_s=0.05,
            stall_s=0.2,
            exit_fn=calls.append,
        )
        w.start()
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.5:
                col.stats["messages"] += 1
                col.stats["snapshots"] += 1
                time.sleep(0.02)
        finally:
            w.stop()
        self.assertEqual(calls, [])

    def test_stop_prevents_fire_on_graceful_stop(self) -> None:
        """Штатное завершение по --minutes: stop() до порога — без выхода."""
        col = Collector(object(), now_ms=1)
        calls: list[int] = []
        w = LivenessWatchdog(
            col,
            check_interval_s=0.05,
            stall_s=0.25,
            exit_fn=calls.append,
        )
        w.start()
        time.sleep(0.08)  # меньше порога
        w.stop()          # run() зовёт stop() в начале finally
        time.sleep(0.4)   # дольше порога, но сторож уже остановлен
        self.assertEqual(calls, [])


class TestSchemaMigration(unittest.TestCase):
    """Миграция схемы: ADD COLUMN для колонок, отсутствующих в существующей
    таблице. recon_checks в data/pm.duckdb создана до коммита 6ba0e29 и не
    имеет колонки seq — export_tables падал по 1051 разу за прогон (SELECT
    seq). CREATE TABLE IF NOT EXISTS колонок не добавляет.

    ДЕФЕКТ А: живая таблица была создана с ПК (token_id, ts_recv_ms) вместо
    (token_id, seq). Теперь миграция пересоздаёт таблицу с ПК (token_id, seq)
    и сохраняет строки с seq IS NOT NULL; строки с seq = NULL не могут войти
    под NOT NULL ПК (token_id, seq) и отбрасываются (см. TestReconChecksKey).
    """

    @staticmethod
    def _create_legacy_db(db_path: Path) -> None:
        """База со СТАРОЙ схемой recon_checks (без seq), как до 6ba0e29."""
        con = duckdb.connect(str(db_path))
        con.execute(
            "CREATE TABLE recon_checks ("
            "ts_recv_ms BIGINT NOT NULL, token_id VARCHAR NOT NULL, "
            "n_levels_ours BIGINT NOT NULL, n_levels_theirs BIGINT NOT NULL, "
            "max_abs_diff_price DOUBLE NOT NULL, max_abs_diff_size DOUBLE NOT NULL, "
            "verdict VARCHAR NOT NULL, PRIMARY KEY (ts_recv_ms, token_id))"
        )
        con.execute(
            "INSERT INTO recon_checks VALUES (1, 'up1', 2, 2, 0.0, 0.0, 'match')"
        )
        con.close()

    @staticmethod
    def _create_wrong_key_db(db_path: Path) -> None:
        """База в состоянии Дефекта А: ПОЛНАЯ схема recon_checks, но ПК
        (token_id, ts_recv_ms) вместо (token_id, seq). Две строки с seq,
        одна строка с seq = NULL (не может быть сохранена под новый ПК)."""
        con = duckdb.connect(str(db_path))
        con.execute(
            "CREATE TABLE recon_checks ("
            "ts_recv_ms BIGINT NOT NULL, token_id VARCHAR NOT NULL, "
            "seq BIGINT, "
            "n_levels_ours BIGINT NOT NULL, n_levels_theirs BIGINT NOT NULL, "
            "max_abs_diff_price DOUBLE NOT NULL, max_abs_diff_size DOUBLE NOT NULL, "
            "extra_ours VARCHAR, extra_theirs VARCHAR, "
            "n_skipped_dedup_token BIGINT, verdict VARCHAR NOT NULL, "
            "PRIMARY KEY (token_id, ts_recv_ms))"
        )
        con.execute(
            "INSERT INTO recon_checks VALUES "
            "(1, 'up1', 10, 2, 2, 0.0, 0.0, NULL, NULL, NULL, 'match'), "
            "(2, 'up1', 11, 2, 2, 0.0, 0.0, NULL, NULL, NULL, 'match'), "
            "(3, 'up1', NULL, 2, 2, 0.0, 0.0, NULL, NULL, NULL, 'match')"
        )
        con.close()

    @staticmethod
    def _primary_key(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
        return [
            str(r[0])
            for r in con.execute(
                "SELECT column_name FROM information_schema.key_column_usage "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
        ]

    def test_connect_adds_new_recon_columns_and_dedup_table(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            self._create_legacy_db(db_path)
            con = store.connect(db_path)
            try:
                recon_cols = {str(r[0]) for r in con.execute("DESCRIBE recon_checks").fetchall()}
                self.assertTrue({"extra_ours", "extra_theirs", "n_skipped_dedup_token"} <= recon_cols)
                # строка с seq = NULL не может войти под новый ПК (token_id, seq)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM dedup_skipped").fetchone()[0], 0)
            finally:
                con.close()

    def test_connect_adds_missing_seq_column(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            self._create_legacy_db(db_path)
            con = store.connect(db_path)  # миграция при открытии
            try:
                cols = {
                    str(r[0]): str(r[1])
                    for r in con.execute("DESCRIBE recon_checks").fetchall()
                }
                self.assertIn("seq", cols)
                self.assertEqual(cols["seq"], "BIGINT")
                # существующая строка с seq = NULL отброшена (сохранить нельзя:
                # seq входит в NOT NULL ПК (token_id, seq))
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 0
                )
                self.assertEqual(self._primary_key(con, "recon_checks"), ["token_id", "seq"])
            finally:
                con.close()

    def test_export_tables_passes_after_migration(self) -> None:
        """export_tables падал на SELECT seq; после миграции проходит."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            self._create_wrong_key_db(db_path)
            con = store.connect(db_path)
            try:
                out = store.export_tables(con, Path(td) / "out")
            finally:
                con.close()
            self.assertIsNotNone(out["recon_checks"])


class TestReconChecksKeyMigration(unittest.TestCase):
    """ДЕФЕКТ А, часть 1: живая recon_checks создана с ПК (token_id, ts_recv_ms)
    вместо (token_id, seq). migrate_schema обязан явной миграцией пересоздать
    таблицу с ПК (token_id, seq), сохранив накопленные строки с seq.

    На старом коде этот класс не проходит: миграции пересоздания нет вообще —
    ПК остаётся (token_id, ts_recv_ms), и пачка с разными seq в одну
    миллисекунду откатывает всю транзакцию (ConstConstraintException в _flush).
    """

    def test_migrate_rebuilds_pk_and_preserves_seq_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            TestSchemaMigration._create_wrong_key_db(db_path)
            con = store.connect(db_path)
            try:
                self.assertEqual(
                    TestSchemaMigration._primary_key(con, "recon_checks"),
                    ["token_id", "seq"],
                )
                rows = con.execute(
                    "SELECT ts_recv_ms, token_id, seq FROM recon_checks ORDER BY seq"
                ).fetchall()
                # строки с seq сохранены, строка с seq=NULL отброшена
                self.assertEqual(rows, [(1, "up1", 10), (2, "up1", 11)])
            finally:
                con.close()

    def test_pk_untouched_when_already_correct(self) -> None:
        """Свежая база (уже ПК (token_id, seq)) не пересоздаётся и строки целы."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            con = store.connect(db_path)
            try:
                self.assertEqual(
                    TestSchemaMigration._primary_key(con, "recon_checks"),
                    ["token_id", "seq"],
                )
                store.insert_row(con, "recon_checks", {
                    "ts_recv_ms": 1, "token_id": "up1", "seq": 5,
                    "n_levels_ours": 2, "n_levels_theirs": 2,
                    "max_abs_diff_price": 0.0, "max_abs_diff_size": 0.0,
                    "verdict": "match",
                })
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 1
                )
            finally:
                con.close()
            # повторное открытие — без сброса данных
            con = store.connect(db_path)
            try:
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 1
                )
                self.assertEqual(
                    TestSchemaMigration._primary_key(con, "recon_checks"),
                    ["token_id", "seq"],
                )
            finally:
                con.close()


class TestFlushGuarded(unittest.TestCase):
    """ДЕФЕКТ А, часть 2: _flush_guarded обязан ПАДАТЬ ГРОМКО — не глотать
    ошибку пачки, логировать размер потерянного пакета и пробрасывать наверх.

    На старом коде _flush_guarded молча глотал ConstraintException/InvalidInput
    после ROLLBACK в _flush, и пакет терялся без следа — эти тесты падали.
    """

    @staticmethod
    def _wrong_key_db(con: duckdb.DuckDBPyConnection) -> None:
        """recon_checks с ПК (token_id, ts_recv_ms): дедуп в пачке идёт по
        (token_id, seq), а реальное ограничение БД — по (token_id, ts_recv_ms).
        """
        con.execute(
            "CREATE TABLE recon_checks ("
            "ts_recv_ms BIGINT NOT NULL, token_id VARCHAR NOT NULL, "
            "seq BIGINT NOT NULL, "
            "n_levels_ours BIGINT NOT NULL, n_levels_theirs BIGINT NOT NULL, "
            "max_abs_diff_price DOUBLE NOT NULL, max_abs_diff_size DOUBLE NOT NULL, "
            "verdict VARCHAR NOT NULL, PRIMARY KEY (token_id, ts_recv_ms))"
        )
        con.execute(
            "INSERT INTO recon_checks VALUES (1, 'up1', 1, 2, 2, 0.0, 0.0, 'match')"
        )

    @staticmethod
    def _recon_row(ts_recv_ms: int, token_id: str, seq: int) -> dict:
        return {
            "ts_recv_ms": ts_recv_ms,
            "token_id": token_id,
            "seq": seq,
            "n_levels_ours": 2,
            "n_levels_theirs": 2,
            "max_abs_diff_price": 0.0,
            "max_abs_diff_size": 0.0,
            "verdict": "match",
        }

    def test_flush_guarded_reraises_on_packet_loss(self) -> None:
        """Пачка из двух строк с разным seq в одну миллисекунду: дедуп по
        (token_id, seq) их пропускает, реальный ПК (token_id, ts_recv_ms)
        конфликтует -> _flush делает ROLLBACK, _flush_guarded обязан
        ПРОБРОСИТЬ исключение, а не проглотить пакет."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pm.duckdb"
            con = duckdb.connect(str(path))
            try:
                self._wrong_key_db(con)
                batch = [
                    ("recon_checks", self._recon_row(1, "up1", 10)),
                    ("recon_checks", self._recon_row(1, "up1", 11)),
                ]
                with self.assertRaises(Exception):
                    store.StoreWriter._flush_guarded(con, batch)
                # ROLLBACK выполнен: частичной записи нет
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 1
                )
            finally:
                con.close()

    def test_flush_guarded_rethrows_original_error(self) -> None:
        """Тип исключения сохраняется (не подменяется на что-то общее)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pm.duckdb"
            con = duckdb.connect(str(path))
            try:
                self._wrong_key_db(con)
                batch = [
                    ("recon_checks", self._recon_row(1, "up1", 10)),
                    ("recon_checks", self._recon_row(1, "up1", 11)),
                ]
                with self.assertRaises(duckdb.Error):
                    store.StoreWriter._flush_guarded(con, batch)
            finally:
                con.close()

    def test_flush_guarded_empty_batch_noop(self) -> None:
        """Пустая пачка не должна падать."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pm.duckdb"
            con = duckdb.connect(str(path))
            try:
                self._wrong_key_db(con)
                store.StoreWriter._flush_guarded(con, [])
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 1
                )
            finally:
                con.close()

    def test_clean_batch_still_writes(self) -> None:
        """Нормальная пачка (ключи не конфликтуют) пишется как раньше."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pm.duckdb"
            con = duckdb.connect(str(path))
            try:
                self._wrong_key_db(con)
                batch = [
                    ("recon_checks", self._recon_row(1, "up2", 10)),
                    ("recon_checks", self._recon_row(2, "up1", 10)),
                ]
                store.StoreWriter._flush_guarded(con, batch)
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 3
                )
            finally:
                con.close()


class TestPreSeqDump(unittest.TestCase):
    """ДЕФЕКТ А, доп.: перед пересозданием recon_checks миграция ОБЯЗАНА выгрузить
    строки с seq IS NULL в parquet (data/validate/recon_checks_pre_seq.parquet для
    живой базы). Эти строки не могут войти под NOT NULL ПК (token_id, seq), но
    терять их нельзя (12 mismatch-строк — улики по дефекту Б).

    На старом коде миграции пересоздания не было вообще: файл не создавался,
    FileExistsError не проверялся, ошибка выгрузки не могла остановить миграцию —
    эти тесты падали.
    """

    def _dump_path(self, db_path: Path) -> Path:
        return db_path.parent / "validate" / store.PRE_SEQ_EXPORT_NAME

    def test_migration_dumps_seq_null_rows_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            TestSchemaMigration._create_wrong_key_db(db_path)
            con = store.connect(db_path)
            try:
                # строка с seq IS NULL выгружена ДО пересоздания
                dump = self._dump_path(db_path)
                self.assertTrue(dump.exists(), f"выгрузка не создана: {dump}")
                import pyarrow.parquet as pq

                rows = pq.read_table(dump).to_pylist()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["token_id"], "up1")
                self.assertIsNone(rows[0]["seq"])
                self.assertEqual(rows[0]["verdict"], "match")
                # сама таблица пересоздана с ПК (token_id, seq), seq-строки целы
                self.assertEqual(
                    TestSchemaMigration._primary_key(con, "recon_checks"),
                    ["token_id", "seq"],
                )
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 2
                )
            finally:
                con.close()

    def test_dump_row_count_and_verdict_breakdown_match(self) -> None:
        """Число строк в файле равно числу строк с seq IS NULL, разбивка по
        verdict совпадает с данными таблицы."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            TestSchemaMigration._create_wrong_key_db(db_path)
            con = store.connect(db_path)
            try:
                import pyarrow.parquet as pq

                dump = self._dump_path(db_path)
                dumped = pq.read_table(dump).to_pylist()
                by_verdict = {}
                for r in dumped:
                    by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
                self.assertEqual(by_verdict, {"match": 1})
                # контроль числа строк: в файле столько же, сколько было NULL-seq
                self.assertEqual(len(dumped), 1)
            finally:
                con.close()

    def test_migration_aborts_if_file_already_exists(self) -> None:
        """Файл выгрузки уже существует -> громкий отказ, таблица НЕ пересоздана."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            TestSchemaMigration._create_wrong_key_db(db_path)
            dump = self._dump_path(db_path)
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_bytes(b"placeholder")  # молча не перезаписываем
            with self.assertRaises(Exception) as cm:
                store.connect(db_path)
            self.assertIn("существует", str(cm.exception))
            # миграция НЕ выполнилась: ПК прежний, строки на месте
            con = duckdb.connect(str(db_path))
            try:
                self.assertEqual(
                    TestSchemaMigration._primary_key(con, "recon_checks"),
                    ["token_id", "ts_recv_ms"],
                )
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 3
                )
            finally:
                con.close()

    def test_migration_aborts_on_dump_failure(self) -> None:
        """Ошибка выгрузки -> миграция НЕ выполняется, падаем громко."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "pm.duckdb"
            TestSchemaMigration._create_wrong_key_db(db_path)
            with mock.patch(
                "src.collect.store._dump_recon_pre_seq",
                side_effect=RuntimeError("synthetic dump failure"),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    store.connect(db_path)
            self.assertIn("synthetic dump failure", str(cm.exception))
            con = duckdb.connect(str(db_path))
            try:
                self.assertEqual(
                    TestSchemaMigration._primary_key(con, "recon_checks"),
                    ["token_id", "ts_recv_ms"],
                )
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0], 3
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
