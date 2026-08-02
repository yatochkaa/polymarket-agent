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
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from src.collect import store
from src.collect.recon import LiveBook, recon_check
from src.collect.ws_collector import (
    Collector,
    BookEvent,
    DeltaEvent,
    TradeEvent,
    interpret_message,
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

    def test_book_message(self) -> None:
        events, _ = interpret_message(BOOK_MSG, ts_recv_ms=1000)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsInstance(ev, BookEvent)
        self.assertEqual(ev.token_id, "up1")
        self.assertEqual(ev.ts_server_ms, 1785651131034)
        self.assertEqual(ev.bids, ((0.01, 3265.0), (0.02, 30000.0), (0.49, 100.0)))

    def test_last_trade_message(self) -> None:
        events, _ = interpret_message(LAST_TRADE_MSG, ts_recv_ms=1000)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], TradeEvent)
        self.assertEqual(events[0].price, 0.981)
        self.assertEqual(events[0].side, "SELL")

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
                raw=json.dumps({"event_type": "price_change", "asset_id": "up1", "ts": 2}),
            )
            col.handle_event(d, 1500)

            b2 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=2000,
                bids=((0.49, 150.0), (0.48, 50.0)),
                asks=((0.50, 200.0), (0.51, 100.0)),
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
                raw=json.dumps({"e": "book", "a": "up1", "t": 1}),
            )
            col.handle_event(b1, 1000)
            # дельта потеряна: следующий снимок уже с размером 300
            b2 = BookEvent(
                token_id="up1", market_id="0x1", ts_server_ms=2000,
                bids=((0.49, 300.0),), asks=((0.50, 200.0),),
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
                raw=json.dumps({"e": "book", "a": "up1", "t": 1}),
            )
            col.handle_event(b1, 1000)
            d = DeltaEvent(
                token_id="up1", market_id="0x1", ts_server_ms=1500,
                side="BUY", price=0.49, size=50.0,
                best_bid=0.49, best_ask=0.50,
                raw=json.dumps({"e": "pc", "a": "up1", "t": 2}),
            )
            col.handle_event(d, 1500)
            col.handle_event(d, 1600)  # дубль (двойная подписка)
            n_snaps = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
            self.assertEqual(n_snaps, 2)  # book + одна дельта, дубль отброшен
            self.assertEqual(col.stats["events_skipped_dedup"], 1)


class TestReconPure(unittest.TestCase):
    def test_warmup_when_not_initialized(self) -> None:
        ours = LiveBook()
        rc = recon_check(ts_recv_ms=1, token_id="up1", ours=ours,
                         theirs_bids={0.49: 100.0}, theirs_asks={0.50: 200.0})
        self.assertEqual(rc["verdict"], "warmup")

    def test_match(self) -> None:
        ours = LiveBook()
        ours.set_book([(0.49, 100.0)], [(0.50, 200.0)])
        rc = recon_check(ts_recv_ms=1, token_id="up1", ours=ours,
                         theirs_bids={0.49: 100.0}, theirs_asks={0.50: 200.0})
        self.assertEqual(rc["verdict"], "match")
        self.assertEqual(rc["max_abs_diff_size"], 0.0)

    def test_mismatch_on_size(self) -> None:
        ours = LiveBook()
        ours.set_book([(0.49, 100.0)], [(0.50, 200.0)])
        rc = recon_check(ts_recv_ms=1, token_id="up1", ours=ours,
                         theirs_bids={0.49: 130.0}, theirs_asks={0.50: 200.0})
        self.assertEqual(rc["verdict"], "mismatch")
        self.assertEqual(rc["max_abs_diff_size"], 30.0)


class TestTickRow(unittest.TestCase):
    def test_verbatim_raw_preserved(self) -> None:
        row = tick_row(
            ts_recv_ms=1, ts_server_ms=2, token_id="up1",
            event_type="price_change", side="BUY", price=0.5, size=10.0,
            best_bid=0.49, best_ask=0.51, raw='{"x":1}', seq=1,
        )
        self.assertEqual(row["raw"], '{"x":1}')
        self.assertEqual(row["event_type"], "price_change")


if __name__ == "__main__":
    unittest.main()
