"""方案 B 足球直播早进场资格单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.config import AppConfig
from src.engine.football_live_entry import (
    football_goal_margin,
    is_football_live_entry_eligible,
)
from src.sports.base import FixtureStatus, FixtureUpdate, SportType


def _football(
    *,
    home_score: int,
    away_score: int,
    elapsed: int | None,
    status: FixtureStatus = FixtureStatus.LIVE,
) -> FixtureUpdate:
    return FixtureUpdate(
        fixture_key="t1",
        sport=SportType.FOOTBALL,
        source_id="test",
        home_team="A",
        away_team="B",
        status=status,
        home_score=home_score,
        away_score=away_score,
        elapsed_minute=elapsed,
        kickoff_time=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
    )


class FootballLiveEntryPlanBTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AppConfig(
            football_min_elapsed_min=80,
            football_blowout_lead=2,
            football_fallback_wallclock_min=95,
        )

    def test_one_goal_never_live_even_at_80(self) -> None:
        """80' 一球领先：不可直播，等终局。"""
        f = _football(home_score=1, away_score=0, elapsed=80)
        self.assertEqual(football_goal_margin(f), 1)
        self.assertFalse(is_football_live_entry_eligible(f, self.cfg))
        f85 = _football(home_score=1, away_score=0, elapsed=85)
        self.assertFalse(is_football_live_entry_eligible(f85, self.cfg))

    def test_two_goal_before_80_not_live(self) -> None:
        """80' 前 2-0 不买。"""
        f = _football(home_score=2, away_score=0, elapsed=79)
        self.assertFalse(is_football_live_entry_eligible(f, self.cfg))

    def test_two_goal_at_80_live(self) -> None:
        """80' 两球领先：可直播。"""
        f = _football(home_score=2, away_score=0, elapsed=80)
        self.assertTrue(is_football_live_entry_eligible(f, self.cfg))

    def test_progression_80_one_goal_then_85_two_goals(self) -> None:
        """80' 1-0 不买 → 85' 2-0 可买（递进）。"""
        at80 = _football(home_score=1, away_score=0, elapsed=80)
        self.assertFalse(is_football_live_entry_eligible(at80, self.cfg))
        at85 = _football(home_score=2, away_score=0, elapsed=85)
        self.assertTrue(is_football_live_entry_eligible(at85, self.cfg))

    def test_progression_82_two_goals_after_late_goal(self) -> None:
        """80' 1-0 → 82' 2-0：分钟与分差同时达标即可买。"""
        at82 = _football(home_score=2, away_score=0, elapsed=82)
        self.assertTrue(is_football_live_entry_eligible(at82, self.cfg))

    def test_margin_shrink_disarms(self) -> None:
        """2-0 @80' 可买；若回退为 1 球差则不再 eligible（防误单）。"""
        ok = _football(home_score=2, away_score=0, elapsed=81)
        self.assertTrue(is_football_live_entry_eligible(ok, self.cfg))
        bad = _football(home_score=2, away_score=1, elapsed=81)
        self.assertFalse(is_football_live_entry_eligible(bad, self.cfg))

    def test_final_not_eligible(self) -> None:
        f = _football(
            home_score=2, away_score=0, elapsed=90, status=FixtureStatus.FINAL
        )
        self.assertFalse(is_football_live_entry_eligible(f, self.cfg))

    def test_three_goal_at_80(self) -> None:
        f = _football(home_score=3, away_score=0, elapsed=80)
        self.assertTrue(is_football_live_entry_eligible(f, self.cfg))


if __name__ == "__main__":
    unittest.main()
