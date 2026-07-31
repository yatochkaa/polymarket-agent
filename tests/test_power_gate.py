from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.experiments.e4_tennis import power_note


class TestPowerGate(unittest.TestCase):
    def test_power_note_reports_singles_match_count_only(self) -> None:
        # Единица = одиночный матч (Уточнение 1, 2026-07-31): power_note
        # сообщает только число матчей и потолок кластеров, не вынося
        # вердикт и не фабрикуя проверку G4 (нет данных по адресам).
        note = power_note(12)
        self.assertIn("12 разрешённых одиночных теннисных матчей", note)
        self.assertIn("Потолок кластеров = 12", note)
        self.assertIn("Гейт G4 (>= 100 матчей на трейдера) на этапе Э4 не проверяется", note)
        self.assertNotIn("UNDECIDABLE", note)
        self.assertNotIn("НЕ ПРОЙДЕН", note)


if __name__ == "__main__":
    unittest.main()
