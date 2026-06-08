"""ESPN NFL scoreboard 数据源。

与 espn_nba.py 结构相同，仅 URL / SportType / league 不同。
无需 API Key，终局只做 Final 判定。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.espn_nfl")

ESPN_NFL_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class EspnNflProvider:
    """ESPN NFL 适配器。"""

    def __init__(
        self,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "espn_nfl"
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if self.limiter is not None and not self.limiter.try_acquire():
            return self._last
        try:
            client = await self.proxy.get_httpx_client()
            resp = await client.get(ESPN_NFL_URL)
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

                home_score_raw = home_c.get("score")
                away_score_raw = away_c.get("score")
                home_score = int(home_score_raw) if home_score_raw not in (None, "") else None
                away_score = int(away_score_raw) if away_score_raw not in (None, "") else None
                winner = None
                if st == FixtureStatus.FINAL and home_score is not None and away_score is not None:
                    if home_score > away_score:
                        winner = "home"
                    elif away_score > home_score:
                        winner = "away"
                    # NFL 极少平局（overtime），若平则 winner=None

                period_raw = status.get("period")
                period = int(period_raw) if isinstance(period_raw, int) else None

                updates.append(
                    FixtureUpdate(
                        fixture_key=f"nfl:espn:{event_id}",
                        sport=SportType.NFL,
                        source_id=self.source_id,
                        home_team=str(home_name),
                        away_team=str(away_name),
                        status=st,
                        home_score=home_score,
                        away_score=away_score,
                        winner=winner,
                        observed_at=datetime.now(timezone.utc),
                        league="nfl",
                        external_id=event_id,
                        period=period,
                        kickoff_time=_parse_dt(event.get("date")),
                    )
                )
            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("espn_nfl 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last
