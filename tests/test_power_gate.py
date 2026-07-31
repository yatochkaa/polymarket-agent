from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.experiments.e4_tennis import power_note


class TestPowerGate(unittest.TestCase):
    def test_power_note_reports_market_count_only(self) -> None:
        note = power_note(12)
        self.assertIn("12 разрешённых рынков", note)
        self.assertIn("Гейт G4 (>= 100 матчей на трейдера) на этапе Э4 не проверяется", note)
        self.assertNotIn("UNDECIDABLE", note)
        self.assertNotIn("НЕ ПРОЙДЕН", note)


if __name__ == "__main__":
    unittest.main()
