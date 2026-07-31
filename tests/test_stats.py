"""Тесты pm.stats: кластерные SE, шринкаж, BH-FDR, решающее правило.

Защищаемые свойства:
- один кластер не даёт конечного SE (inf, а не nan);
- число СДЕЛОК не влияет на SE, влияет только число СОБЫТИЙ;
- shrinkage_fraction всегда в [0, 1] и всегда есть в выводе;
- UNDECIDABLE проверяется ПЕРВЫМ и перебивает GO;
- в критерии СУММА квадратов SE, а не максимум.

Запуск: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm import stats  # noqa: E402


def _cs(mean: float, se: float, n: int = 30) -> stats.ClusterStat:
    """Короткий конструктор ClusterStat для тестов шринкажа."""
    return stats.ClusterStat(mean=mean, se=se, n_clusters=n, n_obs=n * 3)


class TestClusterMeanSe(unittest.TestCase):
    def test_single_cluster_gives_infinite_se(self) -> None:
        """Одно событие = нет информации о разбросе МЕЖДУ событиями."""
        st = stats.cluster_mean_se([0.05, 0.07, 0.09], ["m1", "m1", "m1"])
        self.assertEqual(st.n_clusters, 1)
        self.assertEqual(st.n_obs, 3)
        self.assertTrue(math.isinf(st.se))

    def test_clustering_ignores_trade_count(self) -> None:
        """Добавление сделок внутри тех же событий не сжимает SE.

        Это главная причина, по которой единица наблюдения -- событие.
        """
        few = stats.cluster_mean_se([0.10, -0.02], ["a", "b"])
        many = stats.cluster_mean_se(
            [0.10] * 50 + [-0.02] * 50, ["a"] * 50 + ["b"] * 50
        )
        self.assertEqual(few.n_clusters, many.n_clusters)
        self.assertAlmostEqual(few.se, many.se, places=12)
        self.assertNotEqual(few.n_obs, many.n_obs)

    def test_length_mismatch_and_empty_are_errors(self) -> None:
        with self.assertRaises(ValueError):
            stats.cluster_mean_se([0.1, 0.2], ["a"])
        with self.assertRaises(ValueError):
            stats.cluster_mean_se([], [])


class TestShrink(unittest.TestCase):
    def test_shrinkage_fraction_reported_and_bounded(self) -> None:
        est = stats.shrink(
            {"t1": _cs(0.20, 0.10), "t2": _cs(0.02, 0.10), "t3": _cs(-0.05, 0.10)}
        )
        self.assertEqual(len(est), 3)
        for e in est:
            self.assertGreaterEqual(e.shrinkage_fraction, 0.0)
            self.assertLessEqual(e.shrinkage_fraction, 1.0)

    def test_homogeneous_sample_is_fully_shrunk(self) -> None:
        """Если различия между трейдерами объясняются шумом, tau^2 = 0.

        Тогда shrinkage_fraction = 1.0 у всех и ранжировать НЕЧЕГО.
        Это нормальный и ожидаемый исход для Цели B.
        """
        est = stats.shrink({"a": _cs(0.02, 0.10), "b": _cs(0.01, 0.10)})
        for e in est:
            self.assertEqual(e.shrinkage_fraction, 1.0)

    def test_sorted_by_posterior_not_raw(self) -> None:
        """Ранжирование идёт по постериорному среднему."""
        est = stats.shrink(
            {
                "loud": _cs(0.50, 0.40, n=3),
                "quiet": _cs(0.06, 0.01, n=80),
                "mid": _cs(0.05, 0.01, n=80),
            }
        )
        posts = [e.posterior_mean for e in est]
        self.assertEqual(posts, sorted(posts, reverse=True))

    def test_noisy_estimate_pulled_toward_grand_mean(self) -> None:
        """При реальной гетерогенности (tau^2 > 0) шумный аккаунт сжимается сильнее."""
        by_key = {
            e.key: e
            for e in stats.shrink(
                {
                    "loud": _cs(0.50, 0.40, n=3),
                    "quiet": _cs(0.01, 0.01, n=80),
                    "mid": _cs(0.02, 0.01, n=80),
                    "strong": _cs(0.20, 0.01, n=80),
                }
            )
        }
        self.assertLess(by_key["loud"].posterior_mean, 0.50)
        self.assertGreater(
            by_key["loud"].shrinkage_fraction, by_key["quiet"].shrinkage_fraction
        )

    def test_infinite_se_fully_shrunk(self) -> None:
        """Трейдер с одним событием сжимается полностью, а не возглавляет список."""
        est = {
            e.key: e
            for e in stats.shrink(
                {
                    "one_shot": _cs(0.90, math.inf, n=1),
                    "a": _cs(0.02, 0.01, n=50),
                    "b": _cs(0.03, 0.01, n=50),
                }
            )
        }
        self.assertEqual(est["one_shot"].shrinkage_fraction, 1.0)
        self.assertLess(est["one_shot"].posterior_mean, 0.90)

    def test_all_infinite_se_returns_all_keys(self) -> None:
        est = stats.shrink(
            {"x": _cs(0.1, math.inf, n=1), "y": _cs(0.2, math.inf, n=1)}
        )
        self.assertEqual({e.key for e in est}, {"x", "y"})


class TestBhFdr(unittest.TestCase):
    def test_all_null_rejects_nothing(self) -> None:
        rej = stats.bh_fdr({"a": 0.9, "b": 0.7, "c": 0.5, "d": 0.99}, q=0.10)
        self.assertFalse(any(rej.values()))

    def test_denominator_is_full_family(self) -> None:
        """Добавление неинтересных трейдеров не ослабляет порог."""
        small = stats.bh_fdr({"hit": 0.002}, q=0.10)
        family: dict[str, float] = {"hit": 0.002}
        family.update({f"n{i}": 0.9 for i in range(40)})
        large = stats.bh_fdr(family, q=0.10)
        self.assertTrue(small["hit"])
        self.assertTrue(large["hit"])
        self.assertEqual(sum(large.values()), 1)

    def test_borderline_p_rejected_only_when_below_bh_line(self) -> None:
        """При m=10 и q=0.10 самый малый p должен быть <= 0.01."""
        ps: dict[str, float] = {f"t{i}": 0.5 for i in range(9)}
        ps["edge"] = 0.02
        self.assertFalse(stats.bh_fdr(ps, q=0.10)["edge"])
        ps["edge"] = 0.005
        self.assertTrue(stats.bh_fdr(ps, q=0.10)["edge"])

    def test_invalid_q_and_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            stats.bh_fdr({"a": 0.01}, q=0.0)
        with self.assertRaises(ValueError):
            stats.bh_fdr({"a": 0.01}, q=1.0)
        self.assertEqual(stats.bh_fdr({}, q=0.10), {})


class TestDecide(unittest.TestCase):
    def test_undecidable_beats_go(self) -> None:
        """Широкий bracket перебивает сколь угодно красивое неравенство."""
        d = stats.decide(
            gross_delta=0.05,
            cost=0.001,
            se_edge=0.001,
            se_cost=0.0001,
            bracket_width=0.20,
        )
        self.assertEqual(d.outcome, stats.Outcome.UNDECIDABLE)

    def test_go_when_margin_exceeds_threshold(self) -> None:
        d = stats.decide(
            gross_delta=0.05,
            cost=0.005,
            se_edge=0.005,
            se_cost=0.001,
            bracket_width=0.001,
        )
        self.assertEqual(d.outcome, stats.Outcome.GO)
        self.assertAlmostEqual(d.net, 0.045, places=12)
        self.assertEqual(d.k, 2.5)

    def test_no_go_when_margin_small(self) -> None:
        d = stats.decide(
            gross_delta=0.010,
            cost=0.005,
            se_edge=0.010,
            se_cost=0.002,
            bracket_width=0.001,
        )
        self.assertEqual(d.outcome, stats.Outcome.NO_GO)

    def test_uses_sum_not_max_of_variances(self) -> None:
        """При равных SE сумма даёт порог в sqrt(2) раз выше, чем max.

        Значения подобраны так, что при max был бы GO, а при сумме -- NO-GO.
        Если этот тест упадёт -- критерий тихо ослаблен.
        """
        se = 0.010
        net = 2.5 * se * 1.2
        d = stats.decide(
            gross_delta=net + 0.001,
            cost=0.001,
            se_edge=se,
            se_cost=se,
            bracket_width=0.0,
        )
        self.assertEqual(d.outcome, stats.Outcome.NO_GO)
        self.assertAlmostEqual(d.threshold, 2.5 * math.sqrt(2) * se, places=12)

    def test_infinite_se_never_go(self) -> None:
        d = stats.decide(
            gross_delta=1.0,
            cost=0.0,
            se_edge=math.inf,
            se_cost=0.0,
            bracket_width=0.0,
        )
        self.assertNotEqual(d.outcome, stats.Outcome.GO)

    def test_negative_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            stats.decide(0.05, 0.01, -0.001, 0.001, 0.0)
        with self.assertRaises(ValueError):
            stats.decide(0.05, 0.01, 0.001, 0.001, -0.1)

    def test_only_three_outcomes_exist(self) -> None:
        self.assertEqual(
            {o.value for o in stats.Outcome}, {"GO", "NO-GO", "UNDECIDABLE"}
        )


if __name__ == "__main__":
    unittest.main()
