"""Тесты pm.fees -- единственного источника чисел комиссии.

Главное защищаемое свойство: до закрытия Э2 модуль НЕ имеет права
выдать одно число комиссии тейкера -- только интервал.

Запуск: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm import fees  # noqa: E402

SPORTS = fees.Vertical.SPORTS


class TestFeeRates(unittest.TestCase):
    """Ставки доступны только через fee_rate()."""

    def test_rates_per_vertical(self) -> None:
        self.assertEqual(fees.fee_rate(fees.Vertical.CRYPTO), Decimal("0.07"))
        self.assertEqual(fees.fee_rate(SPORTS), Decimal("0.05"))
        self.assertEqual(fees.fee_rate(fees.Vertical.POLITICS), Decimal("0.04"))
        self.assertEqual(fees.fee_rate(fees.Vertical.GEOPOLITICS), Decimal("0"))

    def test_unknown_vertical_is_error_not_zero(self) -> None:
        """Неизвестная вертикаль -- ошибка классификации, а не нулевая ставка."""
        with self.assertRaises(ValueError):
            fees.fee_rate("tennis")  # type: ignore[arg-type]

    def test_makers_never_pay(self) -> None:
        self.assertEqual(fees.maker_fee(1000, "0.3", SPORTS), Decimal(0))
        self.assertEqual(fees.maker_fee(), Decimal(0))

    def test_rate_table_not_public(self) -> None:
        self.assertNotIn("_TAKER_FEE_RATE", fees.__all__)


class TestBasisGate(unittest.TestCase):
    """Гейт G2: без результата Э2 taker_fee падает, а не угадывает."""

    def test_taker_fee_raises_until_e2_resolved(self) -> None:
        if fees.resolved_basis() != "unknown":
            self.skipTest("Э2 уже закрыт локально -- гейт проверять нечего")
        with self.assertRaises(fees.FeeBasisUnresolved):
            fees.taker_fee(100, "0.2", SPORTS)

    def test_explicit_basis_differs_by_one_over_p(self) -> None:
        """Две трактовки C расходятся ровно в 1/p раз: при p=0.2 это 5x."""
        a = fees.taker_fee(100, "0.2", SPORTS, basis="shares")
        b = fees.taker_fee(100, "0.2", SPORTS, basis="notional")
        self.assertEqual(a / b, Decimal(5))

    def test_price_bounds_enforced(self) -> None:
        for bad in ("0", "1", "1.5"):
            with self.assertRaises(ValueError):
                fees.taker_fee(10, bad, SPORTS, basis="shares")
            with self.assertRaises(ValueError):
                fees.fee_bracket(10, bad, SPORTS)


class TestFeeBracket(unittest.TestCase):
    """fee_bracket всегда вызываем и несёт ширину неопределённости."""

    def test_bracket_available_without_e2(self) -> None:
        q = fees.fee_bracket(100, "0.2", SPORTS)
        self.assertLessEqual(q.low, q.high)
        self.assertEqual(q.bracket_width, q.high - q.low)

    def test_point_is_none_while_unknown(self) -> None:
        if fees.resolved_basis() != "unknown":
            self.skipTest("Э2 уже закрыт локально")
        q = fees.fee_bracket(100, "0.2", SPORTS)
        self.assertIsNone(q.point)
        self.assertEqual(q.basis, "unknown")
        self.assertGreater(q.bracket_width, Decimal(0))

    def test_relative_ambiguity_grows_toward_tails(self) -> None:
        """ОТНОСИТЕЛЬНАЯ неопределённость high/low = 1/p и растёт к краям.

        Отсюда требование Э2 брать сделки с |p-0.5| >= 0.15: там две трактовки
        различимы надёжно.

        ВНИМАНИЕ: АБСОЛЮТНАЯ ширина (high-low) к краям НАОБОРОТ падает,
        потому что сама комиссия содержит множитель p*(1-p). Поэтому в
        решающее правило идёт абсолютная ширина (сравнимая с gross_delta),
        а в отбор сделок для Э2 -- относительная.
        """
        if fees.resolved_basis() != "unknown":
            self.skipTest("Э2 уже закрыт локально")
        for price, expected_ratio in (("0.5", Decimal(2)), ("0.2", Decimal(5)), ("0.05", Decimal(20))):
            q = fees.fee_bracket(100, price, SPORTS)
            self.assertEqual(q.high / q.low, expected_ratio, msg=f"p={price}")

    def test_absolute_width_is_max_near_half(self) -> None:
        """Фиксируем неинтуитивное поведение, чтобы его не "исправили" позже."""
        if fees.resolved_basis() != "unknown":
            self.skipTest("Э2 уже закрыт локально")
        near = fees.fee_bracket(100, "0.5", SPORTS).bracket_width
        far = fees.fee_bracket(100, "0.05", SPORTS).bracket_width
        self.assertGreater(near, far)

    def test_zero_rate_collapses_bracket(self) -> None:
        """Geopolitics: нулевая ставка делает базис C нерелевантным."""
        q = fees.fee_bracket(100, "0.1", fees.Vertical.GEOPOLITICS)
        self.assertEqual(q.bracket_width, Decimal(0))


class TestRecordE2Result(unittest.TestCase):
    """Результат Э2 нельзя тихо переобъявить."""

    def test_write_then_refuse_overwrite(self) -> None:
        with TemporaryDirectory() as d:
            p = Path(d) / "e2_fee_basis.json"
            fees.record_e2_result("shares", {"tx": "0xdeadbeef"}, path=p)
            self.assertEqual(fees.resolved_basis(p), "shares")
            with self.assertRaises(FileExistsError):
                fees.record_e2_result("notional", {"tx": "0xother"}, path=p)

    def test_unknown_basis_rejected(self) -> None:
        with TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                fees.record_e2_result("unknown", {}, path=Path(d) / "x.json")

    def test_missing_file_is_unknown(self) -> None:
        with TemporaryDirectory() as d:
            self.assertEqual(fees.resolved_basis(Path(d) / "nope.json"), "unknown")


if __name__ == "__main__":
    unittest.main()
