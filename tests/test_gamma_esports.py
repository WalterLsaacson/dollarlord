"""Gamma 电竞市场发现单元测试。"""

from src.config import AppConfig
from src.pm.gamma_sync import GammaSync, _event_tags_has_cs2, _is_esports_match_event


class _FakeStore:
    def upsert_market(self, row) -> None:
        pass


def _gamma() -> GammaSync:
    cfg = AppConfig()
    return GammaSync(cfg, None, _FakeStore())  # type: ignore[arg-type]


def test_cs2_tag_counter_strike_2():
    tags = {"games", "esports", "counter-strike-2", "sports"}
    assert _event_tags_has_cs2(tags) is True
    assert _is_esports_match_event(tags) is True


def test_parse_cs2_moneyline_united21():
    g = _gamma()
    event_tags = {"games", "esports", "counter-strike-2", "sports"}
    m = {
        "id": "2010765",
        "question": "Counter-Strike: WRAITH PCIFIC vs UNiTY esports (BO3) - United21 Playoffs",
        "sportsMarketType": "moneyline",
        "clobTokenIds": ["1", "2"],
        "gameStartTime": "2026-06-10T08:00:00+00:00",
        "closed": False,
    }
    row = g._parse_market(
        m,
        "cs2",
        "cs2",
        "Counter-Strike: WRAITH PCIFIC vs UNiTY esports (BO3) - United21 Playoffs",
        "united21 playoffs",
        event_tags,
    )
    assert row is not None
    assert row.sport == "cs2"
    assert row.team_a == "WRAITH PCIFIC"
    assert row.team_b == "UNiTY esports"


def test_parse_lol_moneyline():
    g = _gamma()
    event_tags = {"games", "esports", "league-of-legends", "sports"}
    m = {
        "id": "999",
        "question": "LoL: Saigon Warriors vs Saigon Dino (BO3) - Asia Masters Group C",
        "sportsMarketType": "moneyline",
        "clobTokenIds": ["1", "2"],
        "gameStartTime": "2026-06-10T06:10:00+00:00",
        "closed": False,
    }
    row = g._parse_market(
        m,
        "lol",
        "lol",
        "LoL: Saigon Warriors vs Saigon Dino (BO3) - Asia Masters Group C",
        "asia masters",
        event_tags,
    )
    assert row is not None
    assert row.team_a == "Saigon Warriors"
    assert row.team_b == "Saigon Dino"


def test_exort_series_not_blocked_by_series_keyword():
    """联赛名含 Series 的 CS2 单场不应被 NON_MATCH 误杀。"""
    g = _gamma()
    event_tags = {"games", "esports", "counter-strike-2", "sports"}
    m = {
        "id": "100",
        "question": "Counter-Strike: LPH Gaming vs eSuba (BO3) - Exort Series Main Stage",
        "sportsMarketType": "moneyline",
        "clobTokenIds": ["1", "2"],
        "gameStartTime": "2026-06-10T12:45:00+00:00",
        "closed": False,
    }
    row = g._parse_market(
        m,
        "cs2",
        "cs2",
        "Counter-Strike: LPH Gaming vs eSuba (BO3) - Exort Series Main Stage",
        "exort series main stage",
        event_tags,
    )
    assert row is not None


def test_map_handicap_rejected():
    g = _gamma()
    event_tags = {"games", "esports", "counter-strike-2", "sports"}
    m = {
        "id": "101",
        "question": "Map Handicap: UNiTY (-1.5) vs WRAITH PCIFIC (+1.5)",
        "sportsMarketType": "map_handicap",
        "clobTokenIds": ["1", "2"],
        "gameStartTime": "2026-06-10T08:00:00+00:00",
        "closed": False,
    }
    assert (
        g._parse_market(m, "cs2", "cs2", "title", "text", event_tags) is None
    )
