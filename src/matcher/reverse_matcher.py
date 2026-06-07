"""PM 市场 → 外部赛果反向匹配。"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.engine.kickoff_align import (
    KICKOFF_TOLERANCE_SEC,
    kickoff_delta_sec,
    market_is_future,
    parse_market_kickoff,
)
from src.logging_setup import log_event
from src.store.sqlite import Store

logger = logging.getLogger("arb.matcher")

# 队名清洗
_STRIP_SUFFIX = re.compile(
    r"\b(fc|cf|sc|afc|united|city|club|deportivo)\b",
    re.I,
)
# “Will X win on …?” 盘口：Yes = 题干队 X 赢
_WILL_WIN = re.compile(r"^will\s+(.+?)\s+win\b", re.I)
# 「… end in a draw?」专用平局盘
_DRAW_MARKET = re.compile(r"end in a draw\??\s*$", re.I)


def normalize_team(name: str, store: Store, sport: str) -> str:
    """规范化队名。

    先做 Unicode 重音折叠（如 Tōkyō→tokyo、Ōsaka→osaka、Potosí→potosi、São→sao），
    否则不同数据源的带音标队名会匹配不上（实测 J联赛/巴西/玻利维亚队名常带音标）。
    """
    s = name.lower().strip()
    # NFKD 分解后去掉组合用音标符，把带音标字母折叠成 ASCII
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _STRIP_SUFFIX.sub("", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return store.resolve_alias(s, sport)


def _team_eq(x: str, y: str) -> bool:
    """单队名是否等价：完全相等或较长队名间的子串包含。"""
    if not x or not y:
        return False
    if x == y:
        return True
    if len(x) > 3 and len(y) > 3 and (x in y or y in x):
        return True
    return False


def teams_match(a: str, b: str, c: str, d: str) -> bool:
    """判断 (a,b) 与 (c,d) 是否为同一场比赛（允许主客对调）。

    必须“双方都对应上”才算同一场——只凭一支队伍相同会把不同场次误配
    （例如 “Las Palmas vs Málaga” 误配到 “La Coruña vs Las Palmas”），
    那会导致按错误比赛的赛果下单。
    """
    return (_team_eq(a, c) and _team_eq(b, d)) or (_team_eq(a, d) and _team_eq(b, c))


class ReverseMatcher:
    """将 Polymarket 市场与赛果更新匹配。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        # match_key -> market_id
        self._market_by_match_key: dict[str, str] = {}

    def register_market(
        self,
        market_id: str,
        sport: str,
        team_a: str | None,
        team_b: str | None,
        fixture_key: str | None = None,
    ) -> str | None:
        """尝试为市场建立 match_key。"""
        if not team_a or not team_b:
            return None
        sport_key = "nba" if sport == "nba" else "football"
        ta = normalize_team(team_a, self.store, sport_key)
        tb = normalize_team(team_b, self.store, sport_key)
        match_key = f"{sport_key}:{ta}:{tb}"
        self._market_by_match_key[match_key] = market_id
        self.store.set_market_mapping(
            market_id,
            fixture_key or match_key,
            ["reverse_matcher"],
            watch_state="watching",
        )
        log_event(
            logger,
            "WATCH_ADD",
            market_id=market_id,
            sport=sport,
            team_a=team_a,
            team_b=team_b,
            match_key=match_key,
            source="reverse_matcher",
        )
        return match_key

    def try_match_from_fixtures(
        self,
        market_id: str,
        sport: str,
        team_a: str | None,
        team_b: str | None,
        fixtures: list[FixtureUpdate],
    ) -> str | None:
        """用当前赛果列表反向匹配市场（须开球时间对齐，避免同名不同日）。"""
        if not team_a or not team_b:
            return None
        sport_key = "nba" if sport == "nba" else "football"
        ta = normalize_team(team_a, self.store, sport_key)
        tb = normalize_team(team_b, self.store, sport_key)
        expected_type = SportType.NBA if sport == "nba" else SportType.FOOTBALL
        row = self.store.get_market(market_id)
        market_ko = parse_market_kickoff(row)
        now = datetime.now(timezone.utc)

        best: FixtureUpdate | None = None
        best_delta: float | None = None

        for f in fixtures:
            if f.sport != expected_type:
                continue
            fh = normalize_team(f.home_team, self.store, sport_key)
            fa = normalize_team(f.away_team, self.store, sport_key)
            if not teams_match(ta, tb, fh, fa):
                continue
            # 未来盘不绑定已终局的历史场次（如旧友谊赛 FINAL 污染 6 月 9 日盘）
            if market_is_future(market_ko, now) and f.status == FixtureStatus.FINAL:
                continue
            delta = kickoff_delta_sec(market_ko, f.kickoff_time)
            if market_ko is not None:
                if f.kickoff_time is None:
                    if market_is_future(market_ko, now):
                        continue
                elif delta is None or delta > KICKOFF_TOLERANCE_SEC:
                    continue
            if best_delta is None or (delta is not None and delta < best_delta):
                best_delta = delta
                best = f

        if best is None:
            return None

        fh = normalize_team(best.home_team, self.store, sport_key)
        fa = normalize_team(best.away_team, self.store, sport_key)
        match_key = f"{sport_key}:{fh}:{fa}"
        self._market_by_match_key[match_key] = market_id
        self.store.set_market_mapping(
            market_id,
            best.fixture_key,
            [best.source_id],
            watch_state="watching",
        )
        log_event(
            logger,
            "WATCH_ADD",
            market_id=market_id,
            sport=sport,
            team_a=team_a,
            team_b=team_b,
            match_key=match_key,
            fixture_key=best.fixture_key,
            source=best.source_id,
        )
        return match_key

    def pick_fixture_for_market(
        self,
        row: Any,
        fixtures: list[FixtureUpdate],
    ) -> FixtureUpdate | None:
        """按队名 + 开球时间为 PM 市场匹配唯一赛果（Dashboard / 直播用）。"""
        if row is None:
            return None
        team_a = row["team_a"] if hasattr(row, "__getitem__") else None
        team_b = row["team_b"] if hasattr(row, "__getitem__") else None
        if not team_a or not team_b:
            return None
        sport = str(row["sport"] or "football")
        sport_key = "nba" if sport == "nba" else "football"
        expected_type = SportType.NBA if sport == "nba" else SportType.FOOTBALL
        ta = normalize_team(str(team_a), self.store, sport_key)
        tb = normalize_team(str(team_b), self.store, sport_key)
        market_ko = parse_market_kickoff(row)
        now = datetime.now(timezone.utc)

        best: FixtureUpdate | None = None
        best_delta: float | None = None

        for f in fixtures:
            if f.sport != expected_type:
                continue
            fh = normalize_team(f.home_team, self.store, sport_key)
            fa = normalize_team(f.away_team, self.store, sport_key)
            if not teams_match(ta, tb, fh, fa):
                continue
            if market_is_future(market_ko, now) and f.status == FixtureStatus.FINAL:
                continue
            delta = kickoff_delta_sec(market_ko, f.kickoff_time)
            if market_ko is not None:
                if f.kickoff_time is None:
                    if market_is_future(market_ko, now):
                        continue
                elif delta is None or delta > KICKOFF_TOLERANCE_SEC:
                    continue
            if best_delta is None or (delta is not None and delta < best_delta):
                best_delta = delta
                best = f
        return best

    def market_id_for_final(self, match_key: str) -> str | None:
        return self._market_by_match_key.get(match_key)

    def winner_token_side(
        self,
        market_row: Any,
        winner: str,
        home_team: str | None = None,
        away_team: str | None = None,
    ) -> str | None:
        """根据赛果胜方返回应买入的 token 侧（yes/no）。

        winner: home | away | draw（相对赛果源的 home/away，不是 PM 的 team_a/team_b）
        home_team/away_team: 赛果源主客队名；必传，避免把 PM team_a 误当主场导致买错 token。
        """
        team_a = market_row["team_a"] if hasattr(market_row, "__getitem__") else None
        team_b = market_row["team_b"] if hasattr(market_row, "__getitem__") else None
        if not team_a or not team_b:
            return None
        sport = market_row["sport"]
        ta = normalize_team(str(team_a), self.store, sport)
        tb = normalize_team(str(team_b), self.store, sport)

        if winner == "draw":
            question = str(market_row["question"] or "")
            # 专用平局盘：终局平局 → 买 Yes
            if _DRAW_MARKET.search(question):
                return "yes"
            # 「Will X win?」：平局 = X 未赢 → 买 No（非跳过）
            if _WILL_WIN.search(question):
                return "no"
            return None

        question = str(market_row["question"] or "")
        m = _WILL_WIN.search(question)
        # 胜方队名来自外部赛果（主/客），不能假设 PM team_a == 主场
        ht = normalize_team(home_team or str(team_a), self.store, sport)
        at = normalize_team(away_team or str(team_b), self.store, sport)
        winning_norm = ht if winner == "home" else at

        if m:
            # “Will Guam win?” → Yes=Guam 赢；菲律宾赢则买 No
            subject = normalize_team(m.group(1).strip(), self.store, sport)
            return "yes" if _team_eq(subject, winning_norm) else "no"

        # 单场对阵 “A vs B”：token_yes 通常对应 team_a / outcomes[0]
        if _team_eq(winning_norm, ta):
            return "yes"
        if _team_eq(winning_norm, tb):
            return "no"
        return None
