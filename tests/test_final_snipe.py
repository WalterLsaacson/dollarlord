"""终局抢单待命资格单元测试。"""

from datetime import datetime, timezone

from src.config import AppConfig
from src.engine.final_snipe import is_final_snipe_fixture
from src.sports.base import FixtureStatus, FixtureUpdate, SportType


def _football_live(minute: int) -> FixtureUpdate:
    return FixtureUpdate(
        fixture_key="fb:test",
        sport=SportType.FOOTBALL,
        source_id="api_football",
        home_team="A",
        away_team="B",
        status=FixtureStatus.LIVE,
        home_score=1,
        away_score=0,
        winner=None,
        observed_at=datetime.now(timezone.utc),
        elapsed_minute=minute,
    )


def test_snipe_disabled():
    cfg = AppConfig(final_snipe_enabled=False, final_snipe_minute=75)
    assert is_final_snipe_fixture(_football_live(80), cfg) is False


def test_snipe_before_threshold():
    cfg = AppConfig(final_snipe_enabled=True, final_snipe_minute=80)
    assert is_final_snipe_fixture(_football_live(79), cfg) is False


def test_snipe_at_threshold():
    cfg = AppConfig(final_snipe_enabled=True, final_snipe_minute=80)
    assert is_final_snipe_fixture(_football_live(80), cfg) is True
    assert is_final_snipe_fixture(_football_live(90), cfg) is True


def test_snipe_not_football():
    cfg = AppConfig(final_snipe_enabled=True, final_snipe_minute=75)
    nba = FixtureUpdate(
        fixture_key="nba:test",
        sport=SportType.NBA,
        source_id="espn_nba",
        home_team="A",
        away_team="B",
        status=FixtureStatus.LIVE,
        home_score=100,
        away_score=90,
        winner=None,
        observed_at=datetime.now(timezone.utc),
        period=4,
    )
    assert is_final_snipe_fixture(nba, cfg) is False
