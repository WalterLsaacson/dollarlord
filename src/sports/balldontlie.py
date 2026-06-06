"""balldontlie NBA 免费数据源。

免费层：约 5 次/分钟，需注册获取 API key（请求头 Authorization）。
提供 NBA 当日赛程/比分/状态，作为 ESPN NBA 的交叉验证源。
未配置 key 时该源自动禁用。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.balldontlie")

BDL_BASE = "https://api.balldontlie.io/v1"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class BallDontLieProvider:
    """balldontlie NBA 适配器。"""

    def __init__(
        self,
        cfg: AppConfig,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "balldontlie"
        self.enabled = bool(cfg.balldontlie_key)
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if not self.enabled:
            return []
        if not self.limiter.try_acquire():
            return self._last

        client = await self.proxy.get_httpx_client()
        today = datetime.now(timezone.utc).date().isoformat()
        params = {"dates[]": today}
        headers = {"Authorization": self.cfg.balldontlie_key}
        try:
            resp = await client.get(f"{BDL_BASE}/games", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("balldontlie 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

        updates: list[FixtureUpdate] = []
        for g in data.get("data", []):
            u = self._parse_game(g)
            if u:
                updates.append(u)
        if updates:
            self.store.touch_source(self.source_id)
            self._last = updates
        return updates

    def _parse_game(self, g: dict) -> FixtureUpdate | None:
        home = (g.get("home_team") or {}).get("full_name") or ""
        away = (g.get("visitor_team") or {}).get("full_name") or ""
        if not home or not away:
            return None
        status_raw = str(g.get("status") or "")
        period = g.get("period")
        # status 可能是 "Final"、ISO 时间（未开赛）或 "1st Qtr" 等
        if status_raw.lower() == "final":
            st = FixtureStatus.FINAL
        elif g.get("time") or (isinstance(period, int) and period >= 1):
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        hs = g.get("home_team_score")
        as_ = g.get("visitor_team_score")
        winner = None
        if st == FixtureStatus.FINAL and hs is not None and as_ is not None:
            winner = "home" if hs > as_ else "away" if as_ > hs else "draw"

        kickoff = _parse_dt(status_raw) if re.match(r"\d{4}-\d{2}-\d{2}", status_raw) else _parse_dt(g.get("date"))

        return FixtureUpdate(
            fixture_key=f"nba:bdl:{g.get('id', '')}",
            sport=SportType.NBA,
            source_id=self.source_id,
            home_team=str(home),
            away_team=str(away),
            status=st,
            home_score=int(hs) if hs is not None else None,
            away_score=int(as_) if as_ is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league="nba",
            external_id=str(g.get("id", "")),
            period=int(period) if isinstance(period, int) else None,
            kickoff_time=kickoff,
        )
