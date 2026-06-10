"""TheSportsDB 免费多运动数据源（足球 + 篮球）。

免费层：公共 key "123"，约 30 次/分钟。按日查询赛程/赛果。
免费层不提供精确比赛分钟，用开赛时间墙钟兜底估算进度。
覆盖面广（含世界杯、NBA 等），作为补充交叉验证源。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.thesportsdb")

# 运动名 → 内部 SportType
_SPORT_MAP = {
    "soccer": SportType.FOOTBALL,
    "basketball": SportType.NBA,
}
# 终局/进行中状态关键字
_FINAL_HINTS = ("FT", "MATCH FINISHED", "FINISHED", "AET", "AFTER ET", "FINAL")
_LIVE_HINTS = ("1H", "2H", "HT", "LIVE", "IN PROGRESS", "Q1", "Q2", "Q3", "Q4")


def _parse_dt(date_s: str | None, time_s: str | None) -> datetime | None:
    if not date_s:
        return None
    try:
        ts = f"{date_s}T{(time_s or '00:00:00')}"
        d = datetime.fromisoformat(ts.replace("Z", ""))
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class TheSportsDbProvider:
    """TheSportsDB 适配器。"""

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
        self.source_id = "thesportsdb"
        self.enabled = bool(cfg.thesportsdb_key)
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if not self.enabled:
            return []

        client = await self.proxy.get_httpx_client()
        key = self.cfg.thesportsdb_key
        today = datetime.now(timezone.utc).date().isoformat()
        updates: list[FixtureUpdate] = []
        for sport_name in self.cfg.thesportsdb_sports:
            # 每个运动一次请求，各自占用一个令牌（拿不到则跳过该运动）
            if not self.limiter.try_acquire():
                break
            url = f"https://www.thesportsdb.com/api/v1/json/{key}/eventsday.php"
            params = {"d": today, "s": sport_name}
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.debug("thesportsdb %s 失败: %s", sport_name, e)
                continue
            for ev in (data.get("events") or []):
                u = self._parse_event(ev)
                if u:
                    updates.append(u)
        if updates:
            self.store.touch_source(self.source_id)
            self._last = updates
            # 公共 demo key "123" 每天仅返回极少赛事，无法覆盖非洲/摩洛哥等联赛
            if self.cfg.thesportsdb_key == "123" and len(updates) < 10:
                logger.debug(
                    "thesportsdb demo key 仅 %d 场；升级 Patreon key 可覆盖更多联赛",
                    len(updates),
                )
            return updates
        return self._last

    def _parse_event(self, ev: dict) -> FixtureUpdate | None:
        sport_raw = (ev.get("strSport") or "").lower()
        sport = _SPORT_MAP.get(sport_raw)
        if sport is None:
            return None
        home = ev.get("strHomeTeam") or ""
        away = ev.get("strAwayTeam") or ""
        if not home or not away:
            return None
        status_raw = (ev.get("strStatus") or "").upper().strip()
        if any(h in status_raw for h in _FINAL_HINTS):
            st = FixtureStatus.FINAL
        elif any(h in status_raw for h in _LIVE_HINTS):
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        def _to_int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        hs = _to_int(ev.get("intHomeScore"))
        as_ = _to_int(ev.get("intAwayScore"))
        winner = None
        if st == FixtureStatus.FINAL and hs is not None and as_ is not None:
            if hs > as_:
                winner = "home"
            elif as_ > hs:
                winner = "away"
            else:
                winner = "draw"

        return FixtureUpdate(
            fixture_key=f"{sport.value}:tsdb:{ev.get('idEvent', '')}",
            sport=sport,
            source_id=self.source_id,
            home_team=str(home),
            away_team=str(away),
            status=st,
            home_score=hs,
            away_score=as_,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=str(ev.get("strLeague") or ""),
            external_id=str(ev.get("idEvent", "")),
            kickoff_time=_parse_dt(ev.get("dateEvent"), ev.get("strTime")),
        )
