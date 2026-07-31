from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.experiments.e2_fee_basis import MIN_RESIDUAL_RATIO, report_dict, run
from pm.fees import Vertical, _fee_notional_basis, _fee_shares_basis, fee_rate


class TestE2FeeBasis(unittest.TestCase):
    def _row(self, tx: str, shares: str, price: str, fee: Decimal) -> dict[str, object]:
        return {"tx": tx, "shares": shares, "price": price, "fee": str(fee)}

    def test_run_detects_shares_basis(self) -> None:
        rate = fee_rate(Vertical.SPORTS)
        rows = []
        for i, shares in enumerate((Decimal("10"), Decimal("12"), Decimal("14")), start=1):
            price = Decimal("0.2")
            fee = _fee_shares_basis(shares, price, rate)
            rows.append(self._row(f"0xs{i}", str(shares), str(price), fee))
        r2 = run(None, rows, vertical=Vertical.SPORTS)
        self.assertEqual(r2.basis, "shares")
        self.assertTrue(r2.resolved)

    def test_run_detects_notional_basis(self) -> None:
        rate = fee_rate(Vertical.SPORTS)
        rows = []
        for i, shares in enumerate((Decimal("10"), Decimal("12"), Decimal("14")), start=1):
            price = Decimal("0.2")
            fee = _fee_notional_basis(shares, price, rate)
            rows.append(self._row(f"0xn{i}", str(shares), str(price), fee))
        r2 = run(None, rows, vertical=Vertical.SPORTS)
        self.assertEqual(r2.basis, "notional")
        self.assertTrue(r2.resolved)

    def test_run_returns_unknown_when_residuals_are_comparable(self) -> None:
        rate = fee_rate(Vertical.SPORTS)
        rows = []
        for i, shares in enumerate((Decimal("10"), Decimal("12"), Decimal("14"), Decimal("16")), start=1):
            price = Decimal("0.2")
            a = _fee_shares_basis(shares, price, rate)
            b = _fee_notional_basis(shares, price, rate)
            fee = (a + b) / 2 + Decimal("0.000001") * i
            rows.append(self._row(f"0xu{i}", str(shares), str(price), fee))
        r2 = run(None, rows, vertical=Vertical.SPORTS)
        self.assertEqual(r2.basis, "unknown")
        self.assertFalse(r2.resolved)

    def test_run_returns_unknown_when_ratio_is_too_small(self) -> None:
        rate = fee_rate(Vertical.SPORTS)
        rows = []
        for i, shares in enumerate((Decimal("10"), Decimal("11"), Decimal("12")), start=1):
            price = Decimal("0.2")
            a = _fee_shares_basis(shares, price, rate)
            b = _fee_notional_basis(shares, price, rate)
            fee = a + (b - a) * Decimal("0.4")
            rows.append(self._row(f"0xr{i}", str(shares), str(price), fee))
        r2 = run(None, rows, vertical=Vertical.SPORTS)
        self.assertEqual(r2.basis, "unknown")
        self.assertFalse(r2.resolved)
        self.assertLess(r2.residual_ratio, float(MIN_RESIDUAL_RATIO))

    def test_run_is_monotone_in_better_fit(self) -> None:
        rate = fee_rate(Vertical.SPORTS)
        price = Decimal("0.2")
        rows = [self._row("0xm", "10", str(price), _fee_shares_basis(Decimal("10"), price, rate))]
        r2 = run(None, rows, vertical=Vertical.SPORTS)
        self.assertEqual(r2.basis, "shares")
        self.assertTrue(r2.resolved)

    def test_report_dict_is_json_serializable(self) -> None:
        rate = fee_rate(Vertical.SPORTS)
        rows = [self._row("0x1", "10", "0.2", _fee_shares_basis(Decimal("10"), Decimal("0.2"), rate))]
        r2 = run(None, rows, vertical=Vertical.SPORTS)
        payload = report_dict(r2)
        self.assertIsInstance(payload, dict)
        json.dumps(payload)
        self.assertTrue(all(not isinstance(v, Decimal) for v in payload.values()))
        self.assertTrue(all(not hasattr(v, "dtype") for v in payload.values()))
        self.assertTrue(all(not isinstance(v, dict) for v in payload.values()))


if __name__ == "__main__":
    unittest.main()
