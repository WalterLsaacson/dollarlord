"""API-Football (api-sports.io) 免费足球数据源。

免费层：约 100 次/天、限制 10 次/分钟。提供精确比赛分钟（status.elapsed），
非常适合“开赛 80 分钟后才下单”的资格判断。
鉴权：请求头 x-apisports-key（直连）。未配置 key 时该源自动禁用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.api_football")

# API-Football 状态码
_LIVE_CODES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}
_FINAL_CODES = {"FT", "AET", "PEN"}


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ApiFootballProvider:
    """API-Football 适配器。"""

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
        self.source_id = "api_football"
        self.enabled = bool(cfg.api_football_key)
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if not self.enabled:
            return []
        if not self.limiter.try_acquire():
            return self._last

        client = await self.proxy.get_httpx_client()
        host = self.cfg.api_football_host
        url = f"https://{host}/fixtures"
        # 优先只拉“正在进行”的比赛，省额度；拿不到再退化为当日
        params = {"live": "all"}
        headers = {"x-apisports-key": self.cfg.api_football_key}
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("api_football 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

        updates: list[FixtureUpdate] = []
        for item in data.get("response", []):
            u = self._parse_fixture(item)
            if u:
                updates.append(u)
        if updates:
            self.store.touch_source(self.source_id)
            self._last = updates
        return updates

    def _parse_fixture(self, item: dict) -> FixtureUpdate | None:
        fixture = item.get("fixture") or {}
        teams = item.get("teams") or {}
        goals = item.get("goals") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        if not home or not away:
            return None
        status_obj = fixture.get("status") or {}
        short = (status_obj.get("short") or "").upper()
        elapsed = status_obj.get("elapsed")

        if short in _FINAL_CODES:
            st = FixtureStatus.FINAL
        elif short in _LIVE_CODES:
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        hs = goals.get("home")
        as_ = goals.get("away")
        winner = None
        if st == FixtureStatus.FINAL and hs is not None and as_ is not None:
            if hs > as_:
                winner = "home"
            elif as_ > hs:
                winner = "away"
            else:
                winner = "draw"

        return FixtureUpdate(
            fixture_key=f"fb:apif:{fixture.get('id', '')}",
            sport=SportType.FOOTBALL,
            source_id=self.source_id,
            home_team=str(home),
            away_team=str(away),
            status=st,
            home_score=int(hs) if hs is not None else None,
            away_score=int(as_) if as_ is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=str((item.get("league") or {}).get("id") or ""),
            external_id=str(fixture.get("id", "")),
            elapsed_minute=int(elapsed) if isinstance(elapsed, int) else None,
            kickoff_time=_parse_dt(fixture.get("date")),
        )
