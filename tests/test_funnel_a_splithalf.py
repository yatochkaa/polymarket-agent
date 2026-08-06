"""Юнит-тесты расщепления пополам (probes/deepseek/funnel_a_splithalf.py).

Запуск:
  py -3.13 -u -m pytest tests/test_funnel_a_splithalf.py -v
или:
  py -3.13 -u -m unittest tests.test_funnel_a_splithalf -v
"""

from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from probes.deepseek.funnel_a_splithalf import (  # noqa: E402
    MIN_MATCHES_HALF,
    TIERS,
    betai,
    correlate,
    drop_breakdown,
    half_rows_for_wallet,
    pearson_corr,
    placebo_splithalf_corr,
    rankdata,
    screen_one,
    spearman_corr,
    split_lists_by_gst,
    split_sample,
    split_verdict,
    t_two_sided_p,
    tier_split_state,
    window_bounds,
)
from src.validate.funnel_a import WIN_END_EXCL, WIN_START  # noqa: E402


def _mk_entry(rows):
    """f5-вход кошелька: [(clv, cond, day, gst), ...] -> {clv, cond, day, gst}."""
    return {
        "clv": [r[0] for r in rows],
        "cond": [r[1] for r in rows],
        "day": [r[2] for r in rows],
        "gst": [r[3] for r in rows],
    }


class TestWindowBounds(unittest.TestCase):
    def test_halves_equal_and_match_dates(self):
        start, end, mid = window_bounds()
        self.assertAlmostEqual(mid - start, end - mid, places=6)
        # полная длительность окна = 86 суток
        self.assertAlmostEqual((end - start) / 86400.0, 86.0, places=6)
        self.assertEqual(WIN_START, "2026-02-01")
        self.assertEqual(WIN_END_EXCL, "2026-04-28")


class TestSplitListsByGst(unittest.TestCase):
    def test_boundary_goes_second(self):
        # первая половина gst < mid (строго), gst == mid -> вторая
        entry = _mk_entry([(0.1, "c", "d", 50.0),
                           (0.2, "c", "d", 100.0),
                           (0.3, "c", "d", 150.0)])
        h1, h2, n_placed, n_unplaced = split_lists_by_gst(
            entry["clv"], entry["cond"], entry["day"], entry["gst"], 0.0, 100.0, 200.0)
        self.assertEqual(h1["clv"], [0.1])
        self.assertEqual(h2["clv"], [0.2, 0.3])
        self.assertEqual(n_placed, 3)
        self.assertEqual(n_unplaced, 0)

    def test_none_and_outside_not_placed(self):
        entry = _mk_entry([(0.1, "c", "d", None),
                           (0.2, "c", "d", 50.0),
                           (0.3, "c", "d", 250.0)])  # вне окна
        h1, h2, n_placed, n_unplaced = split_lists_by_gst(
            entry["clv"], entry["cond"], entry["day"], entry["gst"], 0.0, 100.0, 200.0)
        self.assertEqual(h1["clv"], [0.2])
        self.assertEqual(h2["clv"], [])
        self.assertEqual(n_placed, 1)
        self.assertEqual(n_unplaced, 2)

    def test_window_start_inclusive_end_exclusive(self):
        entry = _mk_entry([(0.1, "c", "d", 0.0),
                           (0.2, "c", "d", 199.0),
                           (0.3, "c", "d", 200.0)])  # ровно end -> вне
        h1, h2, n_placed, n_unplaced = split_lists_by_gst(
            entry["clv"], entry["cond"], entry["day"], entry["gst"], 0.0, 100.0, 200.0)
        self.assertEqual(h1["clv"], [0.1])
        self.assertEqual(h2["clv"], [0.2])
        self.assertEqual(n_unplaced, 1)


class TestHalfRowsForWallet(unittest.TestCase):
    def test_counts_and_means(self):
        rows1 = [(0.1, "c%d" % i, "d%d" % (i % 3), 50.0) for i in range(10)]
        rows2 = [(0.3, "c%d" % i, "d%d" % (i % 3), 150.0) for i in range(20)]
        r = half_rows_for_wallet("w", _mk_entry(rows1 + rows2), 0.0, 100.0, 200.0)
        self.assertEqual(r["n1"], 10)
        self.assertEqual(r["n2"], 20)
        self.assertAlmostEqual(r["mean1"], 0.1)
        self.assertAlmostEqual(r["mean2"], 0.3)
        self.assertEqual(r["n_placed"], 30)
        self.assertEqual(r["n_unplaced"], 0)

    def test_same_screen_one_function(self):
        # половина считается ровно тем же screen_one: сверка с прямым вызовом
        clv = [0.1, 0.2, 0.3, 0.4, 0.5]
        entry = _mk_entry([(c, "c%d" % i, "d%d" % (i % 2), 50.0) for i, c in enumerate(clv)])
        r = half_rows_for_wallet("w", entry, 0.0, 100.0, 200.0)
        s = screen_one(clv, ["c%d" % i for i in range(5)],
                       ["d%d" % (i % 2) for i in range(5)])
        self.assertAlmostEqual(r["mean1"], s.mean_clv)
        self.assertAlmostEqual(r["t1"], s.t)


class TestSplitSampleAndDrop(unittest.TestCase):
    def _rows(self):
        return [
            {"wallet": "a", "n1": 60, "n2": 60},
            {"wallet": "b", "n1": MIN_MATCHES_HALF, "n2": MIN_MATCHES_HALF},
            {"wallet": "c", "n1": MIN_MATCHES_HALF - 1, "n2": 60},
            {"wallet": "d", "n1": 60, "n2": MIN_MATCHES_HALF - 1},
            {"wallet": "e", "n1": 10, "n2": 10},
        ]

    def test_sample_includes_only_both_sufficient(self):
        sample = split_sample(self._rows())
        self.assertEqual(sorted(r["wallet"] for r in sample), ["a", "b"])

    def test_drop_breakdown_by_side(self):
        rows = self._rows()
        sample = split_sample(rows)
        d = drop_breakdown(rows, sample)
        self.assertEqual(d["dropped"], 3)
        self.assertEqual(d["n1_only"], 1)   # c
        self.assertEqual(d["n2_only"], 1)   # d
        self.assertEqual(d["both"], 1)      # e

    def test_no_drop_when_all_sufficient(self):
        rows = [{"wallet": "a", "n1": 60, "n2": 60}]
        d = drop_breakdown(rows, split_sample(rows))
        self.assertEqual(d["dropped"], 0)


class TestBeta(unittest.TestCase):
    def test_identities(self):
        for x in (0.1, 0.4, 0.7):
            self.assertAlmostEqual(betai(1, 1, x), x, places=12)
            self.assertAlmostEqual(betai(2, 1, x), x * x, places=12)
            self.assertAlmostEqual(betai(1, 2, x), 1.0 - (1.0 - x) ** 2, places=12)

    def test_edges(self):
        self.assertAlmostEqual(betai(2, 3, 0.0), 0.0, places=12)
        self.assertAlmostEqual(betai(2, 3, 1.0), 1.0, places=12)


class TestTTwoSidedP(unittest.TestCase):
    def test_zero(self):
        self.assertAlmostEqual(t_two_sided_p(0.0, 10), 1.0, places=12)

    def test_cauchy_df1(self):
        # t_1 -- распределение Коши: P(|T| > 1) = 0.5
        self.assertAlmostEqual(t_two_sided_p(1.0, 1), 0.5, places=9)

    def test_known_df18(self):
        # scipy.stats.t.sf(2.0, 18) * 2 ~= 0.06094
        self.assertAlmostEqual(t_two_sided_p(2.0, 18), 0.06094, places=3)

    def test_inf(self):
        self.assertEqual(t_two_sided_p(math.inf, 10), 0.0)

    def test_df_nonpositive(self):
        self.assertTrue(math.isnan(t_two_sided_p(1.0, 0)))


class TestRankdata(unittest.TestCase):
    def test_no_ties(self):
        np.testing.assert_array_equal(rankdata([3.0, 1.0, 2.0]), [3.0, 1.0, 2.0])

    def test_ties_average(self):
        np.testing.assert_array_equal(rankdata([1.0, 1.0, 2.0, 3.0]), [1.5, 1.5, 3.0, 4.0])


class TestPearson(unittest.TestCase):
    def test_matches_numpy_corrcoef(self):
        rng = np.random.default_rng(7)
        x = rng.normal(size=50)
        y = 0.6 * x + 0.8 * rng.normal(size=50)
        r, p = pearson_corr(x, y)
        self.assertAlmostEqual(r, np.corrcoef(x, y)[0, 1], places=12)
        self.assertTrue(math.isfinite(p))

    def test_perfect(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(pearson_corr(x, [2 * v for v in x])[0], 1.0, places=12)
        self.assertAlmostEqual(pearson_corr(x, [-v for v in x])[0], -1.0, places=12)
        self.assertEqual(pearson_corr(x, [2 * v for v in x])[1], 0.0)

    def test_degenerate_nan(self):
        r, p = pearson_corr([1.0, 2.0, 3.0], [1.0, 1.0, 1.0])
        self.assertTrue(math.isnan(r))
        self.assertTrue(math.isnan(p))
        r, p = pearson_corr([1.0], [2.0])
        self.assertTrue(math.isnan(r))


class TestSpearman(unittest.TestCase):
    def test_monotonic_is_one(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [0.1, 0.7, 3.0, 3.5, 9.0]  # монотонно, нелинейно
        r, p = spearman_corr(x, y)
        self.assertAlmostEqual(r, 1.0, places=9)
        self.assertAlmostEqual(p, 0.0, places=9)

    def test_inverse_monotonic(self):
        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        y = [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
        r, _ = spearman_corr(x, y)
        self.assertAlmostEqual(r, -1.0, places=9)


class TestCorrelate(unittest.TestCase):
    def test_known_positive(self):
        rows = [{"mean1": 0.1 * i, "mean2": 0.1 * i + 0.02 * i} for i in range(1, 11)]
        c = correlate(rows)
        self.assertEqual(c["n"], 10)
        self.assertGreater(c["pearson_r"], 0.9)


class TestTierStateAndVerdict(unittest.TestCase):
    def test_support(self):
        self.assertEqual(tier_split_state(0.15, 0.01), "SUPPORT")
        self.assertEqual(tier_split_state(0.9, 1e-9), "SUPPORT")

    def test_null(self):
        self.assertEqual(tier_split_state(0.5, 0.05), "NULL")     # p >= alpha
        self.assertEqual(tier_split_state(0.0, 0.99), "NULL")
        self.assertEqual(tier_split_state(math.nan, math.nan), "NULL")

    def test_ambiguous(self):
        self.assertEqual(tier_split_state(0.1499, 0.01), "AMBIGUOUS")

    def test_verdict_go(self):
        self.assertEqual(split_verdict({"atp": "SUPPORT", "wta": "SUPPORT"}), "GO")

    def test_verdict_no_go(self):
        self.assertEqual(split_verdict({"atp": "NULL", "wta": "NULL"}), "NO-GO")

    def test_verdict_undecidable(self):
        self.assertEqual(split_verdict({"atp": "SUPPORT", "wta": "NULL"}), "UNDECIDABLE")
        self.assertEqual(split_verdict({"atp": "SUPPORT", "wta": "AMBIGUOUS"}), "UNDECIDABLE")
        self.assertEqual(split_verdict({"atp": "NULL", "wta": "AMBIGUOUS"}), "UNDECIDABLE")


class TestEndToEnd(unittest.TestCase):
    def _f5_with_correlated_halves(self):
        """5 кошельков: вторая половина = 0.7*первая + шум (корреляция заметная)."""
        rng = np.random.default_rng(11)
        f5 = {t: {} for t in TIERS}
        for wi in range(5):
            n = 120
            half = n // 2
            clv1 = rng.uniform(-0.05, 0.05, size=half)
            clv2 = 0.7 * clv1 + rng.normal(0.0, 0.01, size=half)
            rows = ([(float(c), "c%d" % i, "d%d" % (i % 5), 50.0) for i, c in enumerate(clv1)] +
                    [(float(c), "c%d" % (i + 1000), "d%d" % ((i + 1000) % 5), 150.0)
                     for i, c in enumerate(clv2)])
            f5["atp"]["w%d" % wi] = _mk_entry(rows)
        return f5

    def test_pipeline_detects_correlation(self):
        f5 = self._f5_with_correlated_halves()
        cand = list(f5["atp"])
        rows = [half_rows_for_wallet(w, f5["atp"][w], 0.0, 100.0, 200.0) for w in cand]
        sample = split_sample(rows)
        self.assertEqual(len(sample), 5)
        c = correlate(sample)
        self.assertGreater(c["pearson_r"], 0.3)
        self.assertLess(c["pearson_p"], 0.05)


class TestPlaceboCalibrationNearZero(unittest.TestCase):
    def test_small_pool_pooled_near_zero(self):
        rng = np.random.default_rng(3)
        pool = []
        for i in range(400):
            pool.append((float(rng.uniform(-0.05, 0.05)),
                         "c%d" % (i % 40),
                         "d%d" % (i % 10),
                         float(rng.uniform(0.0, 200.0))))
        n_sizes = [100, 100, 100, 100]  # сумма 400 == пул
        c = placebo_splithalf_corr(pool, n_sizes, 0.0, 100.0, 200.0)
        self.assertGreater(c["n_included"], 0)
        self.assertTrue(math.isfinite(c["pearson_r"]))
        self.assertLess(abs(c["pearson_r"]), 0.3)


if __name__ == "__main__":
    unittest.main()
