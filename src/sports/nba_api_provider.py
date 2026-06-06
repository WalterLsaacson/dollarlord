"""NBA 官方数据（nba_api 包）。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.nba_api")


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_scoreboard_sync() -> list[FixtureUpdate]:
    """同步拉取今日 NBA 记分牌。"""
    from nba_api.live.nba.endpoints import scoreboard

    board = scoreboard.ScoreBoard()
    data = board.get_dict()
    games = data.get("scoreboard", {}).get("games", [])
    updates: list[FixtureUpdate] = []

    for g in games:
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        home_name = home.get("teamName") or home.get("teamTricode", "")
        away_name = away.get("teamName") or away.get("teamTricode", "")
        game_id = str(g.get("gameId", ""))
        status_raw = (g.get("gameStatusText") or "").strip()
        status_num = g.get("gameStatus")

        if status_raw == "Final" or status_num == 3:
            st = FixtureStatus.FINAL
        elif status_num == 2:
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        home_score = home.get("score")
        away_score = away.get("score")
        winner = None
        if st == FixtureStatus.FINAL and home_score is not None and away_score is not None:
            if home_score > away_score:
                winner = "home"
            elif away_score > home_score:
                winner = "away"

        # 比赛节数（1~4，加时 >4），用于 NBA 早进场资格判断
        period_raw = g.get("period")
        period = int(period_raw) if isinstance(period_raw, int) else None

        fixture_key = f"nba:{game_id}"
        updates.append(
            FixtureUpdate(
                fixture_key=fixture_key,
                sport=SportType.NBA,
                source_id="nba_api",
                home_team=str(home_name),
                away_team=str(away_name),
                status=st,
                home_score=int(home_score) if home_score is not None else None,
                away_score=int(away_score) if away_score is not None else None,
                winner=winner,
                observed_at=datetime.now(timezone.utc),
                league="nba",
                external_id=game_id,
                period=period,
                kickoff_time=_parse_dt(g.get("gameTimeUTC")),
            )
        )
    return updates


class NbaApiProvider:
    """nba_api 适配器。"""

    def __init__(
        self,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "nba_api"
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        # 限流：令牌耗尽则复用上次结果
        if self.limiter is not None and not self.limiter.try_acquire():
            return self._last
        try:
            with self.proxy.requests_env():
                updates = await asyncio.to_thread(_fetch_scoreboard_sync)
            self.store.touch_source("nba_api")
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("nba_api 拉取失败: %s", e)
            self.store.touch_source("nba_api", str(e))
            return self._last
