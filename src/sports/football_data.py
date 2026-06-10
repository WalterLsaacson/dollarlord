"""football-data.org 免费足球数据源。

免费层：10 次/分钟，覆盖 12 项主流赛事（英超/西甲/德甲/意甲/法甲/欧冠/世界杯等）。
鉴权：请求头 X-Auth-Token。未配置 key 时该源自动禁用。
仅用于获取赛程 + 终局比分（免费层不提供精确比赛分钟，用开赛时间墙钟兜底）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.football_data")

FD_BASE = "https://api.football-data.org/v4"
# football-data 的比赛状态映射
_LIVE_STATUS = {"IN_PLAY", "PAUSED", "LIVE"}
_FINAL_STATUS = {"FINISHED", "AWARDED"}


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class FootballDataProvider:
    """football-data.org 适配器。"""

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
        self.source_id = "football_data"
        self.enabled = bool(cfg.football_data_api_key)
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if not self.enabled:
            return []
        # 限流：拿不到令牌则复用上次结果，避免超过 10/min
        if not self.limiter.try_acquire():
            return self._last

        today = datetime.now(timezone.utc).date()
        params = {
            "dateFrom": today.isoformat(),
            "dateTo": (today + timedelta(days=1)).isoformat(),
        }
        headers = {"X-Auth-Token": self.cfg.football_data_api_key}
        try:
            client = await self.proxy.get_httpx_client()
            try:
                resp = await client.get(f"{FD_BASE}/matches", params=params, headers=headers)
            except httpx.RemoteProtocolError:
                # Stale keep-alive connection — reset and retry once
                await self.proxy.aclose()
                client = await self.proxy.get_httpx_client()
                resp = await client.get(f"{FD_BASE}/matches", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("football_data 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

        updates: list[FixtureUpdate] = []
        for m in data.get("matches", []):
            u = self._parse_match(m)
            if u:
                updates.append(u)
        # 请求成功即更新健康状态（即使当日无赛程）
        self.store.touch_source(self.source_id)
        self._last = updates
        return updates

    def _parse_match(self, m: dict) -> FixtureUpdate | None:
        home = (m.get("homeTeam") or {}).get("name") or ""
        away = (m.get("awayTeam") or {}).get("name") or ""
        if not home or not away:
            return None
        raw_status = (m.get("status") or "").upper()
        if raw_status in _FINAL_STATUS:
            st = FixtureStatus.FINAL
        elif raw_status in _LIVE_STATUS:
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        score = (m.get("score") or {}).get("fullTime") or {}
        hs = score.get("home")
        as_ = score.get("away")
        winner = None
        if st == FixtureStatus.FINAL and hs is not None and as_ is not None:
            if hs > as_:
                winner = "home"
            elif as_ > hs:
                winner = "away"
            else:
                winner = "draw"

        return FixtureUpdate(
            fixture_key=f"fb:fd:{m.get('id', '')}",
            sport=SportType.FOOTBALL,
            source_id=self.source_id,
            home_team=str(home),
            away_team=str(away),
            status=st,
            home_score=int(hs) if hs is not None else None,
            away_score=int(as_) if as_ is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=str((m.get("competition") or {}).get("code") or ""),
            external_id=str(m.get("id", "")),
            kickoff_time=_parse_dt(m.get("utcDate")),
        )
