"""Юнит-тесты чистого решающего правила Э1 (Поправка 2).

Эталон last_trade = реальные сделки из /trades. Покрывают условия 1,2,3,5.
Пороги передаются явно, чтобы тесты не зависели от боевых констант.
Запуск: python -u -m unittest tests.test_e1_method -v
"""

import unittest

from pm.experiments.e1_prices_history import (
    BookSample,
    analyze_live,
    select_token_trades,
)

KW = dict(min_divergent=3, align_window_s=90, tick_frac=0.5, match_rate_min=0.9, cross_rate_max=0.1)


def mk_sample(ts: int, mid: float, tick: float = 0.01) -> BookSample:
    return BookSample(
        capture_ms=ts * 1000,
        capture_ts=ts,
        best_bid=mid - tick / 2,
        best_ask=mid + tick / 2,
        mid=mid,
        tick=tick,
        book_last_trade_price=None,  # намеренно НЕ используется
    )


def ramp(n: int, mid: float, tick: float = 0.01, t0: int = 1000, step: int = 30):
    return [mk_sample(t0 + i * step, mid, tick) for i in range(n)]


class TestE1DecisionRule(unittest.TestCase):
    def test_last_trade_detected(self):
        # mid=0.50, реальная сделка=0.40 (расхождение), PH ложится на last_trade
        samples = ramp(6, 0.50)
        trades = [(s.capture_ts - 5, 0.40) for s in samples]
        ph = [(s.capture_ts, 0.40) for s in samples]
        r = analyze_live(samples, ph, trades, **KW)
        self.assertEqual(r.verdict, "last_trade")
        self.assertEqual(r.counts["n_divergent"], 6)
        self.assertEqual(r.match_last_trade_rate, 1.0)

    def test_book_mid_detected(self):
        # mid=0.50, реальная сделка=0.40, PH ложится на mid
        samples = ramp(6, 0.50)
        trades = [(s.capture_ts - 5, 0.40) for s in samples]
        ph = [(s.capture_ts, 0.50) for s in samples]
        r = analyze_live(samples, ph, trades, **KW)
        self.assertEqual(r.verdict, "book_mid")
        self.assertEqual(r.match_mid_rate, 1.0)

    def test_insufficient_divergence_is_inconclusive(self):
        # усл.5: mid == last_trade -> нет расхождения -> n_div=0 < min_divergent -> inconclusive
        samples = ramp(6, 0.50)
        trades = [(s.capture_ts - 5, 0.50) for s in samples]
        ph = [(s.capture_ts, 0.50) for s in samples]
        r = analyze_live(samples, ph, trades, **KW)
        self.assertEqual(r.verdict, "inconclusive")
        self.assertEqual(r.counts["n_divergent"], 0)
        self.assertGreater(r.counts["n_aligned"], 0)

    def test_stale_trade_is_no_data_not_miss(self):
        # усл.3: единственная сделка старше окна 90с -> no_etalon, не промах
        samples = ramp(6, 0.50)
        trades = [(100, 0.40)]  # задолго до снимков (1000+)
        ph = [(s.capture_ts, 0.40) for s in samples]
        r = analyze_live(samples, ph, trades, **KW)
        self.assertEqual(r.verdict, "no_data")
        self.assertEqual(r.counts["n_no_etalon"], 6)
        self.assertEqual(r.counts["n_aligned"], 0)

    def test_no_ph_alignment_is_no_data(self):
        # PH далеко от снимков -> нет выравнивания
        samples = ramp(6, 0.50)
        trades = [(s.capture_ts - 5, 0.40) for s in samples]
        ph = [(999999, 0.40)]
        r = analyze_live(samples, ph, trades, **KW)
        self.assertEqual(r.verdict, "no_data")
        self.assertEqual(r.counts["n_aligned"], 0)

    def test_ambiguous_is_inconclusive(self):
        # mid=0.60, last=0.40 (расходятся), PH=0.50 — между, tol=0.5*0.10=0.05 -> оба мимо
        samples = ramp(6, 0.60, tick=0.10)
        trades = [(s.capture_ts - 5, 0.40) for s in samples]
        ph = [(s.capture_ts, 0.50) for s in samples]
        r = analyze_live(samples, ph, trades, **KW)
        self.assertEqual(r.verdict, "inconclusive")
        self.assertEqual(r.counts["n_ambiguous"], 6)

    def test_empty_is_no_data(self):
        r = analyze_live([], [], [], **KW)
        self.assertEqual(r.verdict, "no_data")

    def test_select_token_trades_filters_by_asset_only(self):
        # усл.1+2: только asset==token; outcomeIndex=999 игнорируется; NO-нога отбрасывается
        rows = [
            {"asset": "YES", "price": "0.40", "timestamp": 1000, "outcomeIndex": 999},
            {"asset": "NO", "price": "0.60", "timestamp": 1001, "outcome": "Yes"},
            {"asset": "YES", "price": "0.41", "timestamp": 1002},
            {"asset": "YES", "price": "bad", "timestamp": 1003},  # парс-ошибка -> пропуск
        ]
        got = select_token_trades(rows, "YES")
        self.assertEqual(got, [(1000, 0.40), (1002, 0.41)])


if __name__ == "__main__":
    unittest.main()
