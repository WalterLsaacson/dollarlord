"""赛果统一数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


class SportType(str, Enum):
    FOOTBALL = "football"
    NBA = "nba"
    MLB = "mlb"
    NHL = "nhl"
    NFL = "nfl"


class FixtureStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINAL = "final"


@dataclass
class FixtureUpdate:
    """单场赛事更新（各数据源归一化）。"""

    fixture_key: str
    sport: SportType
    source_id: str
    home_team: str
    away_team: str
    status: FixtureStatus
    home_score: int | None = None
    away_score: int | None = None
    winner: Literal["home", "away", "draw"] | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    league: str | None = None
    external_id: str | None = None
    # 真实比赛分钟（足球 0~90+，数据源能给则填）
    elapsed_minute: int | None = None
    # 比赛节数（NBA 用，1~4 或更多表示加时）
    period: int | None = None
    # 开赛时间（UTC），用于墙钟兜底估算比赛进度
    kickoff_time: datetime | None = None

    def normalized_home(self) -> str:
        return self.home_team.lower().strip()

    def normalized_away(self) -> str:
        return self.away_team.lower().strip()

    def match_minute_estimate(self, now: datetime | None = None) -> int | None:
        """估算当前比赛进行到的分钟数。

        优先用数据源提供的真实比赛分钟；拿不到时，用“开赛后的墙钟分钟”兜底
        （注意：墙钟会因中场休息、伤停补时而比真实比赛分钟偏大）。
        """
        if self.elapsed_minute is not None:
            return self.elapsed_minute
        if self.kickoff_time is not None and self.status == FixtureStatus.LIVE:
            now = now or datetime.now(timezone.utc)
            kickoff = self.kickoff_time
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            delta_min = (now - kickoff).total_seconds() / 60.0
            if delta_min >= 0:
                return int(delta_min)
        return None


# 足球终局状态码
FOOTBALL_FINAL_CODES = frozenset({"FT", "AET", "PEN", "AWD", "WO"})
