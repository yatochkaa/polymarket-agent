"""Тесты фикса тегированного обхода (пункты 6-7, 2026-07-31)."""

import unittest
from datetime import datetime, timezone

from pm import markets
from pm.experiments import e4_tennis as e4


class TagFilterFix(unittest.TestCase):
    def test_iter_markets_rejects_tag(self) -> None:
        # Тегированный обход через /markets запрещён: сервер молча игнорирует
        # tag_slug. Должен падать, а не возвращать нетегированный список.
        gen = markets.iter_markets(None, tag="tennis")
        with self.assertRaises(RuntimeError):
            next(gen)

    def test_iter_events_requires_tag(self) -> None:
        gen = markets.iter_events(
            None,
            tag="",
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        with self.assertRaises(ValueError):
            next(gen)

    def test_singles_mask(self) -> None:
        self.assertTrue(e4._is_singles_match("atp-almeida-gomez-2026-07-31"))
        self.assertTrue(e4._is_singles_match("wta-swiatek-gauff-2026-06-01"))
        self.assertFalse(e4._is_singles_match("atp-doubles-azkamac-addedeb-2026-07-31"))
        self.assertFalse(e4._is_singles_match("2026-mens-wimbledon-winner"))
        self.assertFalse(e4._is_singles_match("nba-lal-bos-2026-01-01"))
        self.assertFalse(e4._is_singles_match(None))

    def test_doubles_mask(self) -> None:
        self.assertTrue(e4._is_doubles_match("atp-doubles-azkamac-addedeb-2026-07-31"))
        self.assertFalse(e4._is_doubles_match("atp-almeida-gomez-2026-07-31"))

    def test_event_tags_propagated(self) -> None:
        ev = {
            "slug": "atp-x-y-2026-07-31",
            "tags": [{"slug": "tennis"}, "atp"],
            "markets": [
                {
                    "conditionId": "0xabc",
                    "slug": "atp-x-y-2026-07-31",
                    "clobTokenIds": '["1","2"]',
                }
            ],
        }
        ms = markets.markets_from_event(ev)
        self.assertEqual(len(ms), 1)
        self.assertIn("tennis", ms[0].tags)
        self.assertEqual(ms[0].token_ids, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
