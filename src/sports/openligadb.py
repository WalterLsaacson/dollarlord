"""OpenLigaDB 免费足球 API。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.openligadb")

OLB_BASE = "https://api.openligadb.de"


class OpenLigaDbProvider:
    """OpenLigaDB 适配器。"""

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
        self.source_id = "openligadb"
        self._active = set(cfg.openligadb_leagues)
        self._last: list[FixtureUpdate] = []

    def set_active_leagues(self, shortcuts: set[str]) -> None:
        if shortcuts:
            self._active = shortcuts

    async def fetch_updates(self) -> list[FixtureUpdate]:
        updates: list[FixtureUpdate] = []
        client = await self.proxy.get_httpx_client()
        for shortcut in self._active:
            # 每个联赛一次请求各占一个令牌；令牌耗尽则停止本轮，复用缓存
            if self.limiter is not None and not self.limiter.try_acquire():
                break
            url = f"{OLB_BASE}/getmatchdata/{shortcut}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                matches = resp.json()
                if not isinstance(matches, list):
                    continue
                for m in matches:
                    u = self._parse_match(m, shortcut)
                    if u:
                        updates.append(u)
            except Exception as e:
                logger.debug("openligadb %s 失败: %s", shortcut, e)
        if updates:
            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        return self._last

    def _parse_match(self, m: dict, shortcut: str) -> FixtureUpdate | None:
        team1 = m.get("team1", {})
        team2 = m.get("team2", {})
        home = team1.get("teamName", "") if isinstance(team1, dict) else str(team1)
        away = team2.get("teamName", "") if isinstance(team2, dict) else str(team2)
        match_id = str(m.get("matchID", ""))
        finished = bool(m.get("matchIsFinished", False))
        results = m.get("matchResults") or []
        home_score, away_score = 0, 0
        for r in results:
            if r.get("resultName") == "Endergebnis" or r.get("resultTypeID") == 2:
                home_score = int(r.get("pointsTeam1", 0))
                away_score = int(r.get("pointsTeam2", 0))

        if finished:
            st = FixtureStatus.FINAL
        else:
            st = FixtureStatus.LIVE

        winner = None
        if st == FixtureStatus.FINAL:
            if home_score > away_score:
                winner = "home"
            elif away_score > home_score:
                winner = "away"
            else:
                winner = "draw"

        kickoff = None
        raw_dt = m.get("matchDateTimeUTC") or m.get("matchDateTime")
        if raw_dt:
            try:
                kk = datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
                kickoff = kk if kk.tzinfo else kk.replace(tzinfo=timezone.utc)
            except ValueError:
                kickoff = None

        return FixtureUpdate(
            fixture_key=f"fb:olb:{shortcut}:{match_id}",
            sport=SportType.FOOTBALL,
            source_id="openligadb",
            home_team=home,
            away_team=away,
            status=st,
            home_score=home_score,
            away_score=away_score,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=shortcut,
            external_id=match_id,
            kickoff_time=kickoff,
        )
