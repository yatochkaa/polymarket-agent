"""Тесты проверочного контура. Задачи 1-2: discovery, book_poller.

Запуск: python -m unittest discover -s tests -v
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pyarrow.parquet as pq

from src.validate.discovery import (
    NONEXISTENT_TAG,
    GAMMA_BASE_URL,
    DiscoveryResult,
    TagFilterIgnored,
    TennisDiscoveryResult,
    check_tag_filter,
    iter_events,
    tennis_matches,
    updown_outcomes,
)
from src.validate.book_poller import (
    BOOK_SCHEMA,
    ParsedBook,
    build_row,
    parse_book,
    poll_token,
    run_poll,
    vwap_first_qty,
)
from src.validate.compare import (
    PMRow,
    BookLevels,
    BookReconstructor,
    down_transform,
    load_pm_rows,
    compare_side,
)

# Реальные формы из живой пробы 2026-08-02: событие с вложенными рынками,
# clobTokenIds как JSON-строка массива строк, outcomes -- тоже JSON-строка,
# acceptingOrders -- признак живого рынка.
NOW = datetime(2026, 8, 2, 6, 32, 0, tzinfo=timezone.utc)

EV1 = {
    "slug": "ev-btc",
    "closed": False,
    "endDate": "2026-08-02T06:40:00Z",
    "markets": [
        {
            "slug": "btc-updown-5m-1766162100",
            "closed": False,
            "acceptingOrders": True,
            "endDate": "2026-08-02T06:40:00Z",
            "clobTokenIds": '["44594613733704690315394897227759662521183429824282007907633921479634990989260",'
                            '"89077455060153802984632332231552501115671307713789066937817034009529706763206"]',
            "outcomes": '["Up", "Down"]',
        },
        {
            "slug": "btc-updown-5m-1766161500",
            "closed": True,  # закрытый рынок внутри живого события
            "acceptingOrders": False,
            "clobTokenIds": '["s1","s2"]',
            "outcomes": '["Up", "Down"]',
        },
        {
            "slug": "sol-updown-5m-1766162200",
            "closed": False,
            "acceptingOrders": False,  # живое событие, но рынок ордера не принимает
            "endDate": "2026-08-02T06:40:00Z",
            "clobTokenIds": '["z1","z2"]',
            "outcomes": '["Up", "Down"]',
        },
    ],
}
EV2 = {
    "slug": "ev-eth",
    "closed": False,
    "endDate": "2026-08-02T06:35:00Z",
    "markets": [
        {
            "slug": "eth-updown-5m-1766161800",
            "closed": False,
            "acceptingOrders": True,
            "endDate": "2026-08-02T06:35:00Z",
            "clobTokenIds": '["e1","e2"]',
            "outcomes": '["Up", "Down"]',
        },
        {"slug": "eth-anything", "closed": False},  # без маски updown
        {"slug": "sol-updown-5m-1766162100", "closed": False},  # без clobTokenIds
    ],
}
EV_CLOSED = {"slug": "ev-closed", "closed": True, "endDate": "2026-08-02T06:40:00Z", "markets": []}
EV_STALE = {  # endDate далеко за пределами живого окна
    "slug": "ev-stale",
    "closed": False,
    "endDate": "2020-05-05T00:00:00Z",
    "markets": [
        {
            "slug": "btc-updown-5m-1588636800",
            "closed": False,
            "acceptingOrders": True,
            "clobTokenIds": '["q1","q2"]',
            "outcomes": '["Up", "Down"]',
        }
    ],
}

# Теннис: endDate = slug_date + 7 дней (факт замера 2026-08-02). Winner-рынок
# матча имеет slug, РАВНЫЙ slug события; остальные рынки -- с суффиксами.
TENNIS_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
TENNIS_EV = {
    "slug": "atp-sonego-grieksp-2026-08-03",
    "closed": False,
    "endDate": "2026-08-10T14:00:00Z",
    "markets": [
        {  # winner-рынок: slug == slug события
            "slug": "atp-sonego-grieksp-2026-08-03",
            "closed": False,
            "acceptingOrders": True,
            "endDate": "2026-08-10T14:00:00Z",
            "clobTokenIds": '["t1","t2"]',
            "outcomes": '["Lorenzo Sonego", "Tallon Griekspoor"]',
        },
        {"slug": "atp-sonego-grieksp-2026-08-03-completed-match", "closed": False},
        {"slug": "atp-sonego-grieksp-2026-08-03-set-2-winner-Sonego-vs-Griekspoor", "closed": False},
    ],
}
TENNIS_EV_DOUBLES = {  # парный матч: исключается из выборки
    "slug": "atp-doubles-alexgib-chanjoi-2026-07-28",
    "closed": False,
    "endDate": "2026-08-10T15:00:00Z",
    "markets": [
        {
            "slug": "atp-doubles-alexgib-chanjoi-2026-07-28",
            "closed": False,
            "acceptingOrders": True,
            "clobTokenIds": '["d1","d2"]',
            "outcomes": '["A", "B"]',
        }
    ],
}
TENNIS_EV_WRONG_TS = {  # endDate вне окна discovery [now+5d, now+10d]
    "slug": "wta-siegemu-samsono-2026-01-18",
    "closed": False,
    "endDate": "2026-02-01T00:00:00Z",
    "markets": [],
}
TENNIS_EV_NO_WINNER = {  # событие-матч без winner-рынка (нет слага, равного slug события)
    "slug": "itf-noord-urrea-2026-07-29",
    "closed": False,
    "endDate": "2026-08-11T15:00:00Z",
    "markets": [
        {"slug": "itf-noord-urrea-2026-07-29-completed-match", "closed": False},
    ],
}


class FakeGamma:
    """Хендлер MockTransport, повторяющий поведение /events."""

    def __init__(
        self,
        events: list[dict],
        *,
        no_such_tag_returns_data: bool = False,
        ignore_date_filter: bool = False,
    ) -> None:
        self.events = events
        self.no_such_tag_returns_data = no_such_tag_returns_data
        self.ignore_date_filter = ignore_date_filter

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if p.get("tag_slug") == NONEXISTENT_TAG:
            if self.no_such_tag_returns_data:
                return httpx.Response(200, json=[{"slug": "surprise"}])
            return httpx.Response(200, json=[])
        rows = self.events
        lo = p.get("end_date_min")
        hi = p.get("end_date_max")
        if lo and hi and not self.ignore_date_filter:
            rows = [e for e in rows if lo <= (e.get("endDate") or "") <= hi]
        offset = int(p.get("offset", "0") or "0")
        limit = int(p.get("limit", "100") or "100")
        page = rows[offset : offset + limit]
        return httpx.Response(200, json=page)


def make_client(fake: FakeGamma) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(fake.handler),
        base_url=GAMMA_BASE_URL,
        timeout=10.0,
    )


class TestTagFilter(unittest.TestCase):
    def test_nonexistent_tag_returns_empty_ok(self) -> None:
        with make_client(FakeGamma([EV1])) as c:
            self.assertTrue(check_tag_filter(c))

    def test_nonexistent_tag_with_data_raises(self) -> None:
        fake = FakeGamma([EV1], no_such_tag_returns_data=True)
        with make_client(fake) as c:
            with self.assertRaises(TagFilterIgnored):
                check_tag_filter(c)


class TestIterEvents(unittest.TestCase):
    def test_pagination_covers_all_events(self) -> None:
        events = [EV1, EV2, EV_CLOSED]
        # limit=2 -> страницы по 2; closed=false не задаём, closed-событие проходит
        with make_client(FakeGamma(events)) as c:
            got = list(iter_events(c, tag_slug="crypto", closed=None, limit=2))
        self.assertEqual([e["slug"] for e in got], ["ev-btc", "ev-eth", "ev-closed"])

    def test_closed_true_event_under_closed_false_raises(self) -> None:
        # Та же болезнь молчаливого игнорирования фильтров: closed=false запрошен,
        # сервер вернул событие с closed=true -> контракт нарушен.
        with make_client(FakeGamma([EV_CLOSED])) as c:
            with self.assertRaises(TagFilterIgnored):
                list(iter_events(c, tag_slug="crypto", closed=False))

    def test_empty_tag_rejected(self) -> None:
        with make_client(FakeGamma([])) as c:
            with self.assertRaises(ValueError):
                list(iter_events(c, tag_slug="", closed=False))

    def test_date_window_slices_like_server(self) -> None:
        # Окно вокруг NOW покрывает EV1/EV2, EV_STALE (2020) выпадает
        lo = NOW - timedelta(minutes=15)
        hi = NOW + timedelta(minutes=15)
        with make_client(FakeGamma([EV1, EV2, EV_STALE])) as c:
            got = list(
                iter_events(
                    c, tag_slug="crypto", closed=False, end_date_min=lo, end_date_max=hi
                )
            )
        self.assertEqual([e["slug"] for e in got], ["ev-btc", "ev-eth"])

    def test_far_past_window_is_empty(self) -> None:
        lo = datetime(2020, 1, 1, tzinfo=timezone.utc)
        hi = datetime(2020, 1, 2, tzinfo=timezone.utc)
        with make_client(FakeGamma([EV1, EV_STALE])) as c:
            got = list(
                iter_events(
                    c, tag_slug="crypto", closed=False, end_date_min=lo, end_date_max=hi
                )
            )
        self.assertEqual(got, [])

    def test_ignored_date_filter_raises(self) -> None:
        # Сервер игнорирует end_date_min/max (возвращает всё) -> жёсткая остановка
        fake = FakeGamma([EV_STALE], ignore_date_filter=True)
        lo = NOW - timedelta(minutes=15)
        hi = NOW + timedelta(minutes=15)
        with make_client(fake) as c:
            with self.assertRaises(TagFilterIgnored):
                list(
                    iter_events(
                        c, tag_slug="crypto", closed=False, end_date_min=lo, end_date_max=hi
                    )
                )

    def test_date_params_require_pair(self) -> None:
        with make_client(FakeGamma([EV1])) as c:
            with self.assertRaises(ValueError):
                list(iter_events(c, tag_slug="crypto", end_date_min=NOW))


class TestUpdownOutcomes(unittest.TestCase):
    def test_extracts_live_outcomes_only(self) -> None:
        # EV_CLOSED в этом сценарии исключён: он в датном окне и обязан вызвать
        # жёсткую остановку (отдельный тест test_closed_true_events_raise).
        with make_client(FakeGamma([EV1, EV2, EV_STALE])) as c:
            res: DiscoveryResult = updown_outcomes(c, now=NOW)
        slugs = sorted({o.market_slug for o in res.outcomes})
        self.assertEqual(slugs, ["btc-updown-5m-1766162100", "eth-updown-5m-1766161800"])
        # updown рынки окна: btc-live, btc-closed, sol-notlive, eth-live, sol-no-tokens
        self.assertEqual(res.n_updown_markets, 5)
        self.assertEqual(res.n_events_seen, 2)

    def test_stale_event_excluded(self) -> None:
        with make_client(FakeGamma([EV_STALE, EV1])) as c:
            res = updown_outcomes(c, now=NOW)
        self.assertNotIn("btc-updown-5m-1588636800", {o.market_slug for o in res.outcomes})

    def test_token_ids_are_paired_with_outcome_names(self) -> None:
        with make_client(FakeGamma([EV1])) as c:
            res = updown_outcomes(c, now=NOW)
        by_name = {o.outcome: o for o in res.outcomes}
        self.assertEqual(by_name["Up"].token_id,
                         "44594613733704690315394897227759662521183429824282007907633921479634990989260")
        self.assertEqual(by_name["Down"].token_id,
                         "89077455060153802984632332231552501115671307713789066937817034009529706763206")
        self.assertEqual(by_name["Up"].coin, "btc")
        self.assertEqual(by_name["Up"].interval_epoch, 1766162100)

    def test_not_live_market_skipped(self) -> None:
        # sol-updown с acceptingOrders=false не даёт исходов
        with make_client(FakeGamma([EV1])) as c:
            res = updown_outcomes(c, now=NOW)
        self.assertNotIn("sol-updown-5m-1766162200", {o.market_slug for o in res.outcomes})

    def test_market_without_token_ids_yields_no_outcomes(self) -> None:
        with make_client(FakeGamma([EV2])) as c:
            res = updown_outcomes(c, now=NOW)
        # sol-updown без clobTokenIds не должен дать исходов с пустым token_id
        self.assertNotIn("sol-updown-5m-1766162100", {o.market_slug for o in res.outcomes})
        self.assertEqual(len(res.outcomes), 2)  # только eth Up/Down

    def test_empty_events_list(self) -> None:
        with make_client(FakeGamma([])) as c:
            res = updown_outcomes(c, now=NOW)
        self.assertEqual(res.outcomes, ())
        self.assertEqual(res.n_events_seen, 0)

    def test_closed_true_events_raise(self) -> None:
        # Событие closed=true при запросе closed=false -- нарушение контракта
        # фильтра: жёсткая остановка, а не тихий пропуск.
        with make_client(FakeGamma([EV_CLOSED])) as c:
            with self.assertRaises(TagFilterIgnored):
                updown_outcomes(c, now=NOW)


class TestTennisMatches(unittest.TestCase):
    """Теннис: только матчевые winner-рынки одиночек в окне endDate."""

    def test_extracts_match_winners_only(self) -> None:
        # TENNIS_EV в окне (+5..+10 дней) и даёт 2 токена; doubles и stale отсеиваются.
        with make_client(FakeGamma([TENNIS_EV, TENNIS_EV_DOUBLES, TENNIS_EV_WRONG_TS])) as c:
            res: TennisDiscoveryResult = tennis_matches(c, now=TENNIS_NOW)
        self.assertEqual(len(res.matches), 1)
        m = res.matches[0]
        self.assertEqual(m.market_slug, "atp-sonego-grieksp-2026-08-03")
        self.assertEqual(m.token_ids, ("t1", "t2"))
        self.assertEqual(res.n_match_events, 1)
        self.assertEqual(res.n_winner_missing, 0)

    def test_doubles_excluded(self) -> None:
        # Парные матчи (atp-doubles-*) -- вне единицы наблюдения (DECISIONS_NEEDED.md).
        with make_client(FakeGamma([TENNIS_EV_DOUBLES, TENNIS_EV])) as c:
            res = tennis_matches(c, now=TENNIS_NOW)
        slugs = [m.market_slug for m in res.matches]
        self.assertEqual(slugs, ["atp-sonego-grieksp-2026-08-03"])
        self.assertEqual(res.n_match_events, 1)  # doubles не считается матчем

    def test_out_of_window_excluded(self) -> None:
        # TENNIS_EV_WRONG_TS (endDate в 2026-02) вне окна discovery -- сервер не отдаст.
        with make_client(FakeGamma([TENNIS_EV_WRONG_TS, TENNIS_EV])) as c:
            res = tennis_matches(c, now=TENNIS_NOW)
        self.assertEqual(len(res.matches), 1)
        self.assertEqual(res.n_match_events, 1)

    def test_missing_winner_market_counted(self) -> None:
        # Событие-матч без winner-рынка учитывается в n_winner_missing, не падает.
        with make_client(FakeGamma([TENNIS_EV_NO_WINNER])) as c:
            res = tennis_matches(c, now=TENNIS_NOW)
        self.assertEqual(res.matches, ())
        self.assertEqual(res.n_match_events, 1)
        self.assertEqual(res.n_winner_missing, 1)

    def test_empty_events(self) -> None:
        with make_client(FakeGamma([])) as c:
            res = tennis_matches(c, now=TENNIS_NOW)
        self.assertEqual(res.matches, ())
        self.assertEqual(res.n_events_seen, 0)


# --- Задача 2: book_poller -------------------------------------------------

# Реальная форма /book из пробы 2026-08-02: bids по возрастанию, asks по
# убыванию, timestamp -- 13-значное целое (epoch ms), цены/размеры -- строки.
BOOK_FULL = {
    "market": "0xc26c80ef5e01f3946a91958b8b099eb4b25979259eec5413fc01827a10beff54",
    "asset_id": "tok1",
    "timestamp": "1785652861636",
    "hash": "ffc4f8f75b74bcf84ad08efb6b044708f3871084",
    "bids": [
        {"price": "0.01", "size": "5140"},
        {"price": "0.02", "size": "605"},
        {"price": "0.30", "size": "50"},
        {"price": "0.28", "size": "50"},
    ],
    "asks": [
        {"price": "0.99", "size": "5110"},
        {"price": "0.36", "size": "60"},
        {"price": "0.40", "size": "60"},
    ],
    "min_order_size": "5",
    "tick_size": "0.01",
    "neg_risk": False,
    "last_trade_price": "0.630",
}
BOOK_NO_TS = {
    "asset_id": "tok2",
    "bids": [{"price": "0.10", "size": "10"}],
    "asks": [{"price": "0.20", "size": "10"}],
}
BOOK_EMPTY = {"asset_id": "tok3", "bids": [], "asks": []}
BOOK_BIDS_ONLY = {"asset_id": "tok4", "bids": [{"price": "0.10", "size": "10"}], "asks": []}


class TestParseBook(unittest.TestCase):
    def test_full_book(self) -> None:
        pb = parse_book(BOOK_FULL)
        self.assertEqual(pb.server_ts_ms, 1785652861636)
        self.assertEqual(pb.levels_bids, ((0.01, 5140.0), (0.02, 605.0), (0.30, 50.0), (0.28, 50.0)))
        self.assertEqual(pb.levels_asks, ((0.99, 5110.0), (0.36, 60.0), (0.40, 60.0)))
        self.assertFalse(pb.is_empty)
        self.assertFalse(pb.is_incomplete)

    def test_missing_timestamp_is_none(self) -> None:
        pb = parse_book(BOOK_NO_TS)
        self.assertIsNone(pb.server_ts_ms)

    def test_empty_book(self) -> None:
        pb = parse_book(BOOK_EMPTY)
        self.assertTrue(pb.is_empty)
        self.assertFalse(pb.is_incomplete)

    def test_one_sided_book(self) -> None:
        pb = parse_book(BOOK_BIDS_ONLY)
        self.assertTrue(pb.is_incomplete)

    def test_non_dict_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_book([1, 2, 3])
        with self.assertRaises(ValueError):
            parse_book("nope")


class TestVwap(unittest.TestCase):
    def test_exact_100_fill(self) -> None:
        # 50@0.30 + 50@0.28 -> (15+14)/100 = 0.29
        self.assertAlmostEqual(vwap_first_qty([(0.30, 50.0), (0.28, 50.0)], best_first=True), 0.29)

    def test_partial_last_level(self) -> None:
        # 80@0.30 + 20@0.28 -> (24+5.6)/100 = 0.296
        self.assertAlmostEqual(vwap_first_qty([(0.30, 80.0), (0.28, 40.0)], best_first=True), 0.296)

    def test_insufficient_depth_is_none(self) -> None:
        self.assertIsNone(vwap_first_qty([(0.30, 60.0)], best_first=True))
        self.assertIsNone(vwap_first_qty([], best_first=True))

    def test_best_first_orders(self) -> None:
        # bids: лучшая (высшая) цена первой; asks: лучшая (низшая) первой
        bids = [(0.28, 50.0), (0.30, 50.0)]
        asks = [(0.40, 50.0), (0.36, 50.0)]
        self.assertAlmostEqual(vwap_first_qty(bids, best_first=True), 0.29)
        self.assertAlmostEqual(vwap_first_qty(asks, best_first=False), 0.38)  # 50@0.36+50@0.40


class TestBuildRow(unittest.TestCase):
    def test_full_book_row(self) -> None:
        pb = parse_book(BOOK_FULL)
        row = build_row(pb, token_id="tok1", ts_recv_ms=1785652862000, seq=7)
        self.assertEqual(row["ts_recv_ms"], 1785652862000)
        self.assertEqual(row["ts_server_ms"], 1785652861636)
        self.assertEqual(row["token_id"], "tok1")
        self.assertEqual(row["best_bid"], 0.30)   # max по bids, не зависит от порядка
        self.assertEqual(row["best_ask"], 0.36)   # min по asks
        self.assertEqual(row["bid_size"], 50.0)
        self.assertEqual(row["ask_size"], 60.0)
        self.assertAlmostEqual(row["spread"], 0.06)
        self.assertAlmostEqual(row["vwap_bid_100"], 0.29)
        self.assertAlmostEqual(row["vwap_ask_100"], 0.376)  # 60@0.36 + 40@0.40 -> 0.376
        self.assertEqual(row["book_age_ms"], 364)
        self.assertEqual(row["seq"], 7)

    def test_empty_book_row_is_null(self) -> None:
        row = build_row(parse_book(BOOK_EMPTY), token_id="tok3", ts_recv_ms=100, seq=1)
        self.assertIsNone(row["best_bid"])
        self.assertIsNone(row["best_ask"])
        self.assertIsNone(row["bid_size"])
        self.assertIsNone(row["ask_size"])
        self.assertIsNone(row["spread"])
        self.assertIsNone(row["vwap_bid_100"])
        self.assertIsNone(row["vwap_ask_100"])

    def test_no_server_ts_book_age_is_null(self) -> None:
        row = build_row(parse_book(BOOK_NO_TS), token_id="tok2", ts_recv_ms=100, seq=2)
        self.assertIsNone(row["ts_server_ms"])
        self.assertIsNone(row["book_age_ms"])

    def test_one_sided_book_ask_null(self) -> None:
        row = build_row(parse_book(BOOK_BIDS_ONLY), token_id="tok4", ts_recv_ms=100, seq=3)
        self.assertEqual(row["best_bid"], 0.10)
        self.assertIsNone(row["best_ask"])
        self.assertIsNone(row["spread"])
        self.assertIsNone(row["vwap_ask_100"])


def _book_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/book"):
        tok = request.url.params.get("token_id")
        if tok == "bad-token":
            return httpx.Response(500, json={"error": "boom"})
        if tok == "empty":
            return httpx.Response(200, json={"bids": [], "asks": []})
        if tok == "oneside":
            return httpx.Response(200, json={"bids": [{"price": "0.5", "size": "10"}], "asks": []})
        if tok == "noids":
            return httpx.Response(200, json={})  # нет ни bids, ни asks, ни timestamp
        return httpx.Response(
            200,
            json={
                "timestamp": "1785652861636",
                "bids": [{"price": "0.5", "size": "60"}],
                "asks": [{"price": "0.6", "size": "60"}],
            },
        )
    return httpx.Response(404)


class TestPollCycle(unittest.TestCase):
    def test_poll_cycle_writes_schema_rows(self) -> None:
        c = httpx.Client(transport=httpx.MockTransport(_book_handler), timeout=10.0)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "poll.parquet"
            # интервал 0 -> один цикл, 3 токена
            sums = run_poll(
                [(["t1", "t2", "empty"], out)],
                duration_s=100.0,
                interval_s=0.0,
                max_cycles=1,
                retries=1,
                client=c,
            )
            s = sums[0]
            self.assertEqual(s.rows_written, 3)
            self.assertEqual(s.unique_tokens, 3)
            self.assertEqual(s.n_empty, 1)
            self.assertEqual(s.n_failed, 0)
            self.assertEqual(s.cycles, 1)
            self.assertIsNotNone(s.out_path)
            self.assertTrue(Path(s.out_path).exists())
            t = pq.read_table(str(s.out_path))
            self.assertEqual(t.schema.names, list(BOOK_SCHEMA.names))
            rows = t.to_pylist()
            self.assertEqual([r["seq"] for r in rows], [1, 1, 1])
            self.assertEqual({r["token_id"] for r in rows}, {"t1", "t2", "empty"})
            for r in rows:
                self.assertIsNotNone(r["ts_recv_ms"])
            self.assertIsNone(rows[2]["best_bid"])  # empty -> NULL

    def test_one_sided_and_no_ts_counted(self) -> None:
        c = httpx.Client(transport=httpx.MockTransport(_book_handler), timeout=10.0)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "poll.parquet"
            sums = run_poll(
                [(["oneside", "noids"], out)],
                duration_s=100.0,
                interval_s=0.0,
                max_cycles=1,
                retries=1,
                client=c,
            )
            s = sums[0]
            self.assertEqual(s.rows_written, 2)
            self.assertEqual(s.n_incomplete, 1)
            self.assertEqual(s.n_with_server_ts, 0)  # ни у одного нет метки
            rows = pq.read_table(str(s.out_path)).to_pylist()
            self.assertIsNone(rows[0]["best_ask"])
            self.assertIsNone(rows[1]["ts_server_ms"])
            self.assertIsNone(rows[1]["book_age_ms"])

    def test_http_error_is_failed_not_row(self) -> None:
        c = httpx.Client(transport=httpx.MockTransport(_book_handler), timeout=10.0)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "poll.parquet"
            sums = run_poll(
                [(["bad-token"], out)],
                duration_s=100.0,
                interval_s=0.0,
                max_cycles=1,
                retries=1,
                client=c,
            )
            s = sums[0]
            self.assertEqual(s.rows_written, 0)
            self.assertEqual(s.n_failed, 1)
            self.assertIsNone(s.out_path)  # файл не создаётся без строк


# --- Задача 4: compare ------------------------------------------------------

def _pm_book_row(ts_ms: int, bid_prices, bid_sizes, ask_prices, ask_sizes) -> PMRow:
    return PMRow(
        ts_ms=ts_ms,
        event_type="book",
        bid_prices=list(bid_prices), bid_sizes=list(bid_sizes),
        ask_prices=list(ask_prices), ask_sizes=list(ask_sizes),
        pc_price=None, pc_size=None, pc_side=None,
    )


def _pm_pc_row(ts_ms: int, side: str, price: float, size: float) -> PMRow:
    return PMRow(
        ts_ms=ts_ms, event_type="price_change",
        bid_prices=[], bid_sizes=[], ask_prices=[], ask_sizes=[],
        pc_price=price, pc_size=size, pc_side=side,
    )


class TestBookReconstructor(unittest.TestCase):
    def test_book_snapshot_from_arrays(self) -> None:
        # их book: bid_prices по убыванию, ask_prices по возрастанию (лучший = [0])
        r = _pm_book_row(1000, [0.49, 0.48], [126.98, 10.0], [0.5, 0.51], [325.0, 176.98])
        rec = BookReconstructor([r]); rec.seal()
        b = rec.at_or_before(1500)
        self.assertEqual(b.best_bid(), (0.49, 126.98))
        self.assertEqual(b.best_ask(), (0.5, 325.0))
        self.assertAlmostEqual(b.vwap("bid"), 0.49)   # 126.98 >= 100
        self.assertIsNone(b.vwap("bid", 200.0))       # 136.98 < 200

    def test_price_change_updates_one_level(self) -> None:
        rows = [
            _pm_book_row(1000, [0.49, 0.48], [100.0, 50.0], [0.5, 0.51], [100.0, 50.0]),
            _pm_pc_row(2000, "BUY", 0.49, 0.0),        # сняли уровень 0.49 на биде
            _pm_pc_row(3000, "BUY", 0.48, 200.0),      # нарастили 0.48
            _pm_pc_row(4000, "SELL", 0.52, 300.0),     # новый аск 0.52
        ]
        rec = BookReconstructor(rows); rec.seal()
        b = rec.at_or_before(5000)
        self.assertEqual(b.best_bid(), (0.48, 200.0))
        self.assertEqual(b.best_ask(), (0.5, 100.0))   # 0.52 появился, но 0.5 всё ещё лучше
        self.assertEqual(b.asks.get(0.52), 300.0)

    def test_unit_conversion_us_to_ms_is_explicit(self) -> None:
        # load_pm_rows: timestamp мкс (1_500_000_000_000) -> мс (1_500_000_000)
        import pyarrow as pa
        import pyarrow.parquet as pq_tmp
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pm.parquet"
            tbl = pa.Table.from_pylist([{
                "market_slug": "s", "timestamp": 1_500_000_000_000,
                "local_timestamp": 1_500_000_000_000, "event_type": "book",
                "ask_prices": [0.5], "ask_sizes": [10.0],
                "bid_prices": [0.49], "bid_sizes": [10.0],
                "best_ask": None, "best_bid": None,
                "pc_price": None, "pc_size": None, "pc_side": None,
                "new_tick_size": None, "winning_outcome": None,
            }])
            pq_tmp.write_table(tbl, p)
            rows = load_pm_rows(p)
        self.assertEqual(rows[0].ts_ms, 1_500_000_000)


class TestDownComplement(unittest.TestCase):
    def test_prices_and_sizes_swap_sides(self) -> None:
        # Up-книга: bid 0.49@100, ask 0.5@200. Down: bid 0.5@200 (1-0.5), ask 0.51@100.
        up = BookLevels(bids={0.49: 100.0}, asks={0.5: 200.0})
        bid_down, ask_down, book_down = down_transform(
            0.49, 0.5, up
        )
        self.assertEqual(bid_down, 0.5)      # 1 - ask_up
        self.assertEqual(ask_down, 0.51)     # 1 - bid_up
        self.assertEqual(book_down.best_bid(), (0.5, 200.0))   # размер = ask_size_up
        self.assertEqual(book_down.best_ask(), (0.51, 100.0))  # размер = bid_size_up

    def test_missing_side_is_none(self) -> None:
        # bid_up нет -> ask_down = 1 - bid_up = None; ask_up есть -> bid_down = 1 - ask_up
        bid_down, ask_down, book_down = down_transform(None, 0.5)
        self.assertIsNone(ask_down)
        self.assertEqual(bid_down, 0.5)
        self.assertIsNone(book_down)


class TestCompareSide(unittest.TestCase):
    def test_up_side_exact_match(self) -> None:
        rows = [
            _pm_book_row(1000, [0.49, 0.48], [126.98, 10.0], [0.5, 0.51], [325.0, 176.98]),
            _pm_pc_row(2000, "BUY", 0.49, 150.0),
        ]
        snaps = [
            (1500, 0.49, 0.5, 0.49, 0.5),   # book на 1000
            (2500, 0.49, 0.5, 0.49, 0.5),   # после дельты 0.49->150
        ]
        report, mism = compare_side(rows_pm=rows, snapshots=snaps, side_is_down=False)
        self.assertEqual(report["best_bid"]["matched"], 2)
        self.assertEqual(report["best_bid"]["over_tick"], 0)
        self.assertEqual(report["vwap_bid_100"]["matched"], 2)
        self.assertEqual(mism, [])

    def test_down_side_via_complement(self) -> None:
        rows = [
            _pm_book_row(1000, [0.49, 0.48], [126.98, 10.0], [0.5, 0.51], [325.0, 176.98]),
        ]
        # наши Down-снимки: bid=0.5 (1-их ask), ask=0.51 (1-их bid)
        snaps = [(1500, 0.5, 0.51, 0.5, 0.51)]
        report, mism = compare_side(rows_pm=rows, snapshots=snaps, side_is_down=True)
        self.assertEqual(report["best_bid"]["matched"], 1)
        self.assertEqual(report["best_bid"]["exact_share"], 1.0)
        self.assertEqual(report["best_ask"]["exact_share"], 1.0)
        self.assertEqual(mism, [])

    def test_mismatch_reported_when_over_tick(self) -> None:
        rows = [_pm_book_row(1000, [0.49], [100.0], [0.5], [100.0])]
        snaps = [(1500, 0.49, 0.60, None, None)]   # ask разъехался на 0.10
        report, mism = compare_side(rows_pm=rows, snapshots=snaps, side_is_down=False)
        self.assertEqual(report["best_ask"]["over_tick"], 1)
        self.assertEqual(report["best_ask"]["exact_share"], 0.0)
        self.assertEqual(report["best_bid"]["over_tick"], 0)
        # расхождение и по best_ask, и по spread (оба > tick)
        self.assertEqual(len(mism), 2)
        ask_mism = [m for m in mism if m.metric == "best_ask"]
        self.assertEqual(len(ask_mism), 1)
        self.assertEqual(ask_mism[0].ts_server_ms, 1500)
        self.assertAlmostEqual(ask_mism[0].ours, 0.60)
        self.assertAlmostEqual(ask_mism[0].theirs, 0.5)


if __name__ == "__main__":
    unittest.main()