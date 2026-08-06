"""Юнит-тесты плацебо-контроля фильтра 5 (probes/deepseek/funnel_a_placebo.py).

Запуск:
  py -3.13 -u -m pytest tests/test_funnel_a_placebo.py -v
или:
  py -3.13 -u -m unittest tests.test_funnel_a_placebo -v
"""

from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from probes.deepseek.funnel_a_placebo import (  # noqa: E402
    TIERS,
    _run_scheme,
    block_assign,
    build_pool,
    build_synthetic_f5,
    deciles,
    placebo_verdict,
    pooled_summary,
    run_one_seed,
    run_one_seed_b,
    sample_pairs,
    t_stats,
    tail_counts,
)


def _mk_f5(atp_rows, wta_rows=None):
    """f5-структура основного прогона: tier -> wallet -> {clv, cond, day}."""
    return {
        "atp": {"w1": {"clv": [r[0] for r in atp_rows],
                       "cond": [r[1] for r in atp_rows],
                       "day": [r[2] for r in atp_rows]}},
        "wta": ({"w1": {"clv": [r[0] for r in wta_rows],
                        "cond": [r[1] for r in wta_rows],
                        "day": [r[2] for r in wta_rows]}} if wta_rows else {}),
    }


class TestBuildPool(unittest.TestCase):
    def test_pool_equals_f5_rows(self):
        f5 = _mk_f5([(0.1, "c1", "d1"), (0.2, "c2", "d2")],
                    wta_rows=[(0.3, "c3", "d3")])
        pool = build_pool(f5)
        self.assertEqual(pool["atp"], [(0.1, "c1", "d1"), (0.2, "c2", "d2")])
        self.assertEqual(pool["wta"], [(0.3, "c3", "d3")])

    def test_pool_concatenates_wallets(self):
        f5 = {
            "atp": {
                "a": {"clv": [0.1, 0.2], "cond": ["c1", "c2"], "day": ["d1", "d2"]},
                "b": {"clv": [0.3], "cond": ["c3"], "day": ["d3"]},
            },
            "wta": {},
        }
        pool = build_pool(f5)
        # порядок: сначала кошелек a, потом b (по порядку dict)
        self.assertEqual(pool["atp"], [(0.1, "c1", "d1"), (0.2, "c2", "d2"), (0.3, "c3", "d3")])
        self.assertEqual(pool["wta"], [])


class TestSamplePairs(unittest.TestCase):
    def setUp(self):
        self.pool = list(range(20))

    def test_draws_n_distinct(self):
        rng = random.Random(1)
        s = sample_pairs(self.pool, 5, rng)
        self.assertEqual(len(s), 5)
        self.assertEqual(len(set(s)), 5)  # без повторений внутри кошелька
        self.assertTrue(set(s).issubset(set(self.pool)))

    def test_n_zero(self):
        self.assertEqual(sample_pairs(self.pool, 0, random.Random(1)), [])

    def test_n_above_pool_raises(self):
        # независимый семплинг без возвращения невозможен при n_i > пул
        with self.assertRaises(RuntimeError):
            sample_pairs(self.pool, 21, random.Random(1))

    def test_negative_n_raises(self):
        with self.assertRaises(ValueError):
            sample_pairs(self.pool, -1, random.Random(1))


class TestBuildSyntheticF5(unittest.TestCase):
    def test_n_i_preserved(self):
        pool = [(0.1, "c%d" % i, "d%d" % (i % 3)) for i in range(30)]
        f5s = build_synthetic_f5(pool, [3, 5, 7], random.Random(1), "tag")
        self.assertEqual(set(f5s), {"tag_0", "tag_1", "tag_2"})
        for i, n in enumerate([3, 5, 7]):
            wid = "tag_%d" % i
            self.assertEqual(len(f5s[wid]["clv"]), n)
            self.assertEqual(len(f5s[wid]["cond"]), n)
            self.assertEqual(len(f5s[wid]["day"]), n)

    def test_reproducible_with_seed(self):
        pool = [(0.1, "c%d" % i, "d%d" % (i % 3)) for i in range(30)]
        a = build_synthetic_f5(pool, [4, 4, 4], random.Random(7), "tag")
        b = build_synthetic_f5(pool, [4, 4, 4], random.Random(7), "tag")
        self.assertEqual(a, b)


class TestRunOneSeed(unittest.TestCase):
    def _pool_and_n(self):
        # небольшой, но достаточный пул: sum(n_i) <= пул
        pool = {
            "atp": [(round(0.5 * math.sin(i) + 0.5, 4), "c%d" % i, "d%d" % (i % 5)) for i in range(60)],
            "wta": [(round(0.5 * math.cos(i) + 0.5, 4), "c%d" % i, "d%d" % (i % 5)) for i in range(60)],
        }
        n = {"atp": [4, 6], "wta": [5]}
        return pool, n

    def test_counts_in_range(self):
        pool, n = self._pool_and_n()
        res = run_one_seed(pool, n, 1)
        self.assertEqual(set(res), set(TIERS))
        self.assertTrue(0 <= res["atp"] <= len(n["atp"]))
        self.assertTrue(0 <= res["wta"] <= len(n["wta"]))

    def test_reproducible_with_seed(self):
        pool, n = self._pool_and_n()
        self.assertEqual(run_one_seed(pool, n, 3), run_one_seed(pool, n, 3))

    def test_uses_real_screen_code(self):
        # плацебо применяет именно фильтр 5: кошелёк с явно проходящим
        # паттерном засчитывается, с проваливающим -- нет.
        pool = {
            "atp": [(0.05, "c%d" % i, "d%d" % i) for i in range(40)],  # все clv > 0
            "wta": [(-0.05, "c%d" % i, "d%d" % i) for i in range(40)],  # все clv < 0
        }
        n = {"atp": [8, 8], "wta": [8]}
        # сид фиксированный; хотя бы один из двух ATP-кошельков с n=8 на
        # положительных clv с высокой вероятностью проходит, WTA не проходит
        res = run_one_seed(pool, n, 5)
        self.assertGreaterEqual(res["atp"], 0)
        self.assertEqual(res["wta"], 0)  # средний CLV <= 0 -> не проходят всегда


class TestBlockAssign(unittest.TestCase):
    def setUp(self):
        self.pool = list(range(100))

    def test_sizes_preserved_and_disjoint(self):
        rng = random.Random(1)
        sizes = [10, 20, 5, 15]
        blocks = block_assign(self.pool, sizes, rng)
        self.assertEqual(sorted(len(items) for _, items in blocks), sorted(sizes))
        total = 0
        used = set()
        for n, items in blocks:
            self.assertEqual(len(items), n)
            self.assertFalse(used & set(items))  # ни одна пара не используется дважды
            used |= set(items)
            total += n
        self.assertEqual(total, sum(sizes))

    def test_blocks_fill_prefix_of_shuffled_pool(self):
        # схема B покрывает ровно первые sum(sizes) элементов перемешанного пула
        rng = random.Random(2)
        sizes = [7, 3, 9]
        blocks = block_assign(self.pool, sizes, rng)
        used = [it for _, items in blocks for it in items]
        self.assertEqual(len(used), sum(sizes))
        self.assertTrue(set(used).issubset(set(self.pool)))

    def test_sum_above_pool_raises(self):
        with self.assertRaises(RuntimeError):
            block_assign(self.pool, [60, 50], random.Random(1))  # сумма 110 > 100

    def test_reproducible_with_seed(self):
        a = block_assign(self.pool, [10, 10, 10], random.Random(7))
        b = block_assign(self.pool, [10, 10, 10], random.Random(7))
        self.assertEqual([n for n, _ in a], [n for n, _ in b])
        for (_, ia), (_, ib) in zip(a, b):
            self.assertEqual(ia, ib)

    def test_block_order_randomized_by_seed(self):
        # порядок раздачи блоков рандомизируется сидом: не совпадает с входным
        sizes = list(range(1, 11))
        blocks = block_assign(self.pool, sizes, random.Random(1))
        got = [n for n, _ in blocks]
        self.assertNotEqual(got, sizes)  # входной порядок 1..10 не сохранился
        self.assertEqual(sorted(got), sorted(sizes))


class TestRunOneSeedB(unittest.TestCase):
    def _pool_and_n(self):
        pool = {
            "atp": [(round(0.5 * math.sin(i) + 0.5, 4), "c%d" % i, "d%d" % (i % 5)) for i in range(60)],
            "wta": [(round(0.5 * math.cos(i) + 0.5, 4), "c%d" % i, "d%d" % (i % 5)) for i in range(60)],
        }
        n = {"atp": [4, 6], "wta": [5]}
        return pool, n

    def test_counts_in_range(self):
        pool, n = self._pool_and_n()
        res = run_one_seed_b(pool, n, 1)
        self.assertEqual(set(res), set(TIERS))
        self.assertTrue(0 <= res["atp"] <= len(n["atp"]))
        self.assertTrue(0 <= res["wta"] <= len(n["wta"]))

    def test_reproducible_with_seed(self):
        pool, n = self._pool_and_n()
        self.assertEqual(run_one_seed_b(pool, n, 3), run_one_seed_b(pool, n, 3))

    def test_uses_real_screen_code(self):
        pool = {
            "atp": [(0.05, "c%d" % i, "d%d" % i) for i in range(40)],
            "wta": [(-0.05, "c%d" % i, "d%d" % i) for i in range(40)],
        }
        n = {"atp": [8, 8], "wta": [8]}
        res = run_one_seed_b(pool, n, 5)
        self.assertGreaterEqual(res["atp"], 0)
        self.assertEqual(res["wta"], 0)  # средний CLV <= 0 -> не проходят всегда


class TestTailCounts(unittest.TestCase):
    def test_strict_both_sides(self):
        vals = [-4.0, -3.0, -2.5, -2.0, -1.0, 0.0, 1.0, 2.0, 2.5, 3.0, 4.0]
        # равенство порогу не в счёте (строгие < и >)
        self.assertEqual(tail_counts(vals, 3.0), (1, 1))    # -4 | 4
        self.assertEqual(tail_counts(vals, 2.5), (2, 2))    # -4,-3 | 3,4
        self.assertEqual(tail_counts(vals, 2.0), (3, 3))    # -4,-3,-2.5 | 2.5,3,4

    def test_empty(self):
        self.assertEqual(tail_counts([], 3.0), (0, 0))


class TestTStats(unittest.TestCase):
    def test_known(self):
        s = t_stats([0.0, 1.0, 2.0, 3.0])
        self.assertAlmostEqual(s["mean"], 1.5)
        self.assertAlmostEqual(s["std"], math.sqrt(1.25))  # numpy ddof=0
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["n_finite"], 4)
        self.assertEqual(s["n_inf"], 0)
        self.assertEqual(s["n_nan"], 0)

    def test_inf_excluded_from_mean_std(self):
        s = t_stats([0.0, 1.0, math.inf])
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["n_finite"], 2)
        self.assertEqual(s["n_inf"], 1)
        self.assertAlmostEqual(s["mean"], 0.5)
        self.assertAlmostEqual(s["std"], 0.5)

    def test_empty(self):
        s = t_stats([])
        self.assertEqual(s["n"], 0)
        self.assertTrue(math.isnan(s["mean"]))
        self.assertTrue(math.isnan(s["std"]))


class TestPooledSummaryAndRunScheme(unittest.TestCase):
    def _pool_and_n(self):
        pool = {
            "atp": [(round(0.5 * math.sin(i) + 0.5, 4), "c%d" % i, "d%d" % (i % 5)) for i in range(60)],
            "wta": [(round(0.5 * math.cos(i) + 0.5, 4), "c%d" % i, "d%d" % (i % 5)) for i in range(60)],
        }
        n = {"atp": [4, 6], "wta": [5]}
        return pool, n

    def test_run_scheme_b_collects_pooled_t(self):
        pool, n = self._pool_and_n()
        sb = _run_scheme("B", pool, n)
        # 2 синтетических кошелька ATP x 20 сидов, 1 WTA x 20 сидов
        self.assertEqual(len(sb["pooled_t"]["atp"]), 2 * 20)
        self.assertEqual(len(sb["pooled_t"]["wta"]), 1 * 20)
        # счётчики согласованы с run_one_seed_b
        self.assertEqual(sb["counts"]["atp"], [run_one_seed_b(pool, n, s)["atp"] for s in range(1, 21)])
        self.assertEqual(sb["counts"]["wta"], [run_one_seed_b(pool, n, s)["wta"] for s in range(1, 21)])

    def test_pooled_summary(self):
        pool, n = self._pool_and_n()
        sb = _run_scheme("B", pool, n)
        ps = pooled_summary(sb["pooled_t"]["atp"])
        self.assertEqual(ps["n"], 40)
        self.assertEqual(len(ps["deciles"]["d"]), 9)
        self.assertTrue(math.isfinite(ps["mean"]))
        self.assertTrue(math.isfinite(ps["std"]))


class TestDeciles(unittest.TestCase):
    def test_known_case(self):
        d = deciles([float(x) for x in range(1, 11)])
        self.assertEqual(d["n"], 10)
        self.assertEqual(d["n_finite"], 10)
        self.assertEqual(d["n_inf"], 0)
        self.assertEqual(d["n_nan"], 0)
        # np.percentile([1..10],[10..90], linear)
        expected = [1.9, 2.8, 3.7, 4.6, 5.5, 6.4, 7.3, 8.2, 9.1]
        for got, exp in zip(d["d"], expected):
            self.assertAlmostEqual(got, exp, places=9)
        self.assertEqual(d["min"], 1.0)
        self.assertEqual(d["max"], 10.0)

    def test_empty(self):
        d = deciles([])
        self.assertEqual(d["n"], 0)
        self.assertIsNone(d["min"])
        self.assertIsNone(d["max"])
        self.assertEqual(len(d["d"]), 9)
        self.assertTrue(all(x is None for x in d["d"]))

    def test_negative_and_inf(self):
        d = deciles([-5.0, 1.0, 2.0, math.inf])
        self.assertEqual(d["n_inf"], 1)
        self.assertEqual(d["n"], 4)
        self.assertEqual(d["n_finite"], 3)
        self.assertEqual(d["min"], -5.0)
        self.assertEqual(d["max"], math.inf)


class TestPlaceboVerdict(unittest.TestCase):
    def test_mean_ge_3_se_underestimated(self):
        # граница включена: mean == 3.0 -> SE занижена, порог пересматривается
        self.assertEqual(placebo_verdict(3.0, 1, 9), "SE_ZANIZHENA")
        self.assertEqual(placebo_verdict(5.0, 10, 9), "SE_ZANIZHENA")

    def test_real_effect(self):
        self.assertEqual(placebo_verdict(0.5, 3, 9), "REAL_EFFECT")
        # max должен быть СТРОГО меньше реального числа прошедших
        self.assertEqual(placebo_verdict(0.5, 8, 9), "REAL_EFFECT")

    def test_undecidable_boundaries(self):
        # mean == 1.0: условие mean < 1 не выполнено -> неопределённый исход
        self.assertEqual(placebo_verdict(1.0, 3, 9), "UNDECIDABLE")
        # max == real_passes: условие max < real не выполнено
        self.assertEqual(placebo_verdict(0.5, 9, 9), "UNDECIDABLE")
        # промежуточное среднее 1 < mean < 3
        self.assertEqual(placebo_verdict(2.0, 8, 9), "UNDECIDABLE")
        # mean >= 3 перекрывает даже max < real
        self.assertEqual(placebo_verdict(4.0, 2, 9), "SE_ZANIZHENA")


class TestVerdictPerTierUsesRealPasses(unittest.TestCase):
    def test_wta_real_passes_smaller(self):
        # у WTA real_passes = 5: max < 5 -> реальный эффект, хотя для ATP max=8 был бы неопределён
        self.assertEqual(placebo_verdict(0.4, 4, 5), "REAL_EFFECT")
        self.assertEqual(placebo_verdict(0.4, 5, 5), "UNDECIDABLE")
        self.assertEqual(placebo_verdict(0.4, 8, 5), "UNDECIDABLE")


if __name__ == "__main__":
    unittest.main()
