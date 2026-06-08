"""MLB StatsAPI（statsapi.mlb.com）官方免费数据源。

拉取今日 + 昨日赛程，覆盖跨午夜结束的比赛。
无需 API Key，延迟约 1-3 秒，终局只做 Final 判定。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.mlb_statsapi")

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class MlbStatsApiProvider:
    """MLB StatsAPI 适配器。"""

    def __init__(
        self,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "mlb_statsapi"
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if self.limiter is not None and not self.limiter.try_acquire():
            return self._last
        now = datetime.now(timezone.utc)
        dates = [
            (now - timedelta(days=1)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        ]
        updates: list[FixtureUpdate] = []
        seen: set[str] = set()
        try:
            client = await self.proxy.get_httpx_client()
            for date_str in dates:
                resp = await client.get(
                    MLB_SCHEDULE_URL,
                    params={"sportId": "1", "date": date_str, "hydrate": "linescore"},
                )
                resp.raise_for_status()
                for date_block in resp.json().get("dates", []):
                    for game in date_block.get("games", []):
                        u = self._parse_game(game)
                        if u and u.external_id not in seen:
                            seen.add(u.external_id or "")
                            updates.append(u)
            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("mlb_statsapi 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

    def _parse_game(self, game: dict[str, Any]) -> FixtureUpdate | None:
        teams = game.get("teams") or {}
        home_info = teams.get("home") or {}
        away_info = teams.get("away") or {}
        home_name = (home_info.get("team") or {}).get("name") or ""
        away_name = (away_info.get("team") or {}).get("name") or ""
        if not home_name or not away_name:
            return None

        game_pk = str(game.get("gamePk", ""))
        status = game.get("status") or {}
        abstract_state = (status.get("abstractGameState") or "").lower()
        # abstractGameState: "Final" | "Live" | "Preview"
        if abstract_state == "final":
            st = FixtureStatus.FINAL
        elif abstract_state == "live":
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        home_score = home_info.get("score")
        away_score = away_info.get("score")
        winner = None
        if st == FixtureStatus.FINAL and home_score is not None and away_score is not None:
            if int(home_score) > int(away_score):
                winner = "home"
            elif int(away_score) > int(home_score):
                winner = "away"
            # MLB 不会平局（加局制）

        # 当前局数：linescore.currentInning
        linescore = game.get("linescore") or {}
        inning = linescore.get("currentInning")
        period = int(inning) if inning is not None else None

        return FixtureUpdate(
            fixture_key=f"mlb:statsapi:{game_pk}",
            sport=SportType.MLB,
            source_id=self.source_id,
            home_team=str(home_name),
            away_team=str(away_name),
            status=st,
            home_score=int(home_score) if home_score is not None else None,
            away_score=int(away_score) if away_score is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league="mlb",
            external_id=game_pk,
            period=period,
            kickoff_time=_parse_dt(game.get("gameDate")),
        )
