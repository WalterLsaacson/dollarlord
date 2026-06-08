"""NHL 官方 API（api-web.nhle.com）数据源。

使用 /v1/score/YYYY-MM-DD 拉取指定日期赛事分数。
拉取今日 + 昨日，覆盖跨午夜结束的比赛。无需 API Key。
终局只做 Final 判定（gameState: OFF / FINAL / OVER）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.nhl_api")

NHL_SCORE_URL = "https://api-web.nhle.com/v1/score/{date}"

# gameState 终局码
_NHL_FINAL_STATES = frozenset({"off", "final", "over"})
# gameState 直播码
_NHL_LIVE_STATES = frozenset({"live", "crit"})


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _team_name(team: dict) -> str:
    """从 homeTeam/awayTeam 块提取队名（优先 fullName，次选 name.default，再选 abbrev）。"""
    if not team:
        return ""
    full = team.get("fullName") or ""
    if full:
        return str(full)
    name_block = team.get("name") or {}
    default = name_block.get("default") or ""
    if default:
        return str(default)
    return str(team.get("abbrev") or "")


class NhlApiProvider:
    """NHL 官方 API 适配器。"""

    def __init__(
        self,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "nhl_api"
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
                url = NHL_SCORE_URL.format(date=date_str)
                resp = await client.get(url)
                resp.raise_for_status()
                for game in resp.json().get("games", []):
                    u = self._parse_game(game)
                    if u and u.external_id not in seen:
                        seen.add(u.external_id or "")
                        updates.append(u)
            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("nhl_api 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

    def _parse_game(self, game: dict) -> FixtureUpdate | None:
        home_info = game.get("homeTeam") or {}
        away_info = game.get("awayTeam") or {}
        home_name = _team_name(home_info)
        away_name = _team_name(away_info)
        if not home_name or not away_name:
            return None

        game_id = str(game.get("id", ""))
        raw_state = (game.get("gameState") or "").lower()
        if raw_state in _NHL_FINAL_STATES:
            st = FixtureStatus.FINAL
        elif raw_state in _NHL_LIVE_STATES:
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        home_score = home_info.get("score")
        away_score = away_info.get("score")
        winner = None
        if st == FixtureStatus.FINAL and home_score is not None and away_score is not None:
            hs, as_ = int(home_score), int(away_score)
            if hs > as_:
                winner = "home"
            elif as_ > hs:
                winner = "away"

        period_desc = game.get("periodDescriptor") or {}
        period_raw = period_desc.get("number")
        period = int(period_raw) if period_raw is not None else None

        return FixtureUpdate(
            fixture_key=f"nhl:api:{game_id}",
            sport=SportType.NHL,
            source_id=self.source_id,
            home_team=str(home_name),
            away_team=str(away_name),
            status=st,
            home_score=int(home_score) if home_score is not None else None,
            away_score=int(away_score) if away_score is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league="nhl",
            external_id=game_id,
            period=period,
            kickoff_time=_parse_dt(game.get("startTimeUTC")),
        )
