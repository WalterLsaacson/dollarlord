"""ESPN NBA scoreboard。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.espn_nba")

ESPN_NBA_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class EspnNbaProvider:
    """ESPN NBA 适配器。"""

    def __init__(
        self,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "espn_nba"
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        # 限流：令牌耗尽则复用上次结果
        if self.limiter is not None and not self.limiter.try_acquire():
            return self._last
        try:
            client = await self.proxy.get_httpx_client()
            resp = await client.get(ESPN_NBA_URL)
            resp.raise_for_status()
            data = resp.json()
            updates: list[FixtureUpdate] = []
            for event in data.get("events", []):
                comp = (event.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
                home_name = home_c.get("team", {}).get("displayName", "")
                away_name = away_c.get("team", {}).get("displayName", "")
                event_id = str(event.get("id", ""))
                status = comp.get("status", {})
                status_type = status.get("type", {})
                completed = status_type.get("completed", False)
                state = status_type.get("state", "")

                if completed or state == "post":
                    st = FixtureStatus.FINAL
                elif state == "in":
                    st = FixtureStatus.LIVE
                else:
                    st = FixtureStatus.SCHEDULED

                home_score = int(home_c.get("score", 0) or 0)
                away_score = int(away_c.get("score", 0) or 0)
                winner = None
                if st == FixtureStatus.FINAL:
                    if home_score > away_score:
                        winner = "home"
                    elif away_score > home_score:
                        winner = "away"

                period_raw = status.get("period")
                period = int(period_raw) if isinstance(period_raw, int) else None

                updates.append(
                    FixtureUpdate(
                        fixture_key=f"nba:espn:{event_id}",
                        sport=SportType.NBA,
                        source_id="espn_nba",
                        home_team=home_name,
                        away_team=away_name,
                        status=st,
                        home_score=home_score,
                        away_score=away_score,
                        winner=winner,
                        observed_at=datetime.now(timezone.utc),
                        league="nba",
                        external_id=event_id,
                        period=period,
                        kickoff_time=_parse_dt(event.get("date")),
                    )
                )
            self.store.touch_source("espn_nba")
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("espn_nba 拉取失败: %s", e)
            self.store.touch_source("espn_nba", str(e))
            return self._last
