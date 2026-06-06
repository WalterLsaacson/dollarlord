"""ESPN 足球 scoreboard（按联赛 slug）。"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FOOTBALL_FINAL_CODES, FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.espn_soccer")


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_minute(status: dict) -> int | None:
    """从 ESPN status 中解析比赛分钟（displayClock 如 "82'"、"90'+3"）。"""
    disp = str(status.get("displayClock") or "")
    m = re.match(r"\s*(\d+)", disp)
    if m:
        base = int(m.group(1))
        plus = re.search(r"\+\s*(\d+)", disp)
        return base + (int(plus.group(1)) if plus else 0)
    clock = status.get("clock")
    if isinstance(clock, (int, float)) and clock > 0:
        return int(clock)
    return None


class EspnSoccerProvider:
    """ESPN 足球适配器。"""

    def __init__(
        self,
        cfg: AppConfig,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "espn_soccer"
        self._active_leagues: list[str] = list(cfg.espn_soccer_leagues)
        # 轮询轮转起点：联赛多 + 限流时，确保各联赛轮流刷新，不会饿死靠后的
        self._rr = 0
        self._last: list[FixtureUpdate] = []

    def set_active_leagues(self, leagues) -> None:
        """仅轮询 watchlist 涉及的联赛。"""
        leagues = list(leagues)
        if leagues:
            self._active_leagues = leagues
            self._rr = 0

    async def fetch_updates(self) -> list[FixtureUpdate]:
        updates: list[FixtureUpdate] = []
        client = await self.proxy.get_httpx_client()
        leagues = self._active_leagues
        n = len(leagues)
        # 从上次轮转位置开始，保证多联赛在限流下能轮流被刷新
        order = leagues[self._rr:] + leagues[: self._rr] if n else []
        polled = 0
        for league in order:
            # 每个联赛一次请求各占一个令牌；令牌耗尽则停止本轮，复用缓存
            if self.limiter is not None and not self.limiter.try_acquire():
                break
            polled += 1
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                for event in data.get("events", []):
                    u = self._parse_event(event, league)
                    if u:
                        updates.append(u)
            except Exception as e:
                logger.debug("espn_soccer %s 失败: %s", league, e)
        if n:
            # 下一轮从本轮未覆盖到的联赛继续
            self._rr = (self._rr + max(polled, 1)) % n
        if updates:
            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        return self._last

    def _parse_event(self, event: dict, league: str) -> FixtureUpdate | None:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        home_name = home_c.get("team", {}).get("displayName", "")
        away_name = away_c.get("team", {}).get("displayName", "")
        event_id = str(event.get("id", ""))
        status = comp.get("status", {})
        short = (status.get("type", {}).get("shortDetail") or status.get("type", {}).get("name") or "")
        state = status.get("type", {}).get("state", "")
        completed = status.get("type", {}).get("completed", False)

        if completed or state == "post" or any(code in short.upper() for code in FOOTBALL_FINAL_CODES):
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
            else:
                winner = "draw"

        elapsed = _parse_minute(status) if st == FixtureStatus.LIVE else None

        return FixtureUpdate(
            fixture_key=f"fb:espn:{league}:{event_id}",
            sport=SportType.FOOTBALL,
            source_id="espn_soccer",
            home_team=home_name,
            away_team=away_name,
            status=st,
            home_score=home_score,
            away_score=away_score,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=league,
            external_id=event_id,
            elapsed_minute=elapsed,
            kickoff_time=_parse_dt(event.get("date")),
        )
