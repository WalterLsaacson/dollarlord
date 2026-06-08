"""Riot LoL Esports 半官方 API（esports-api.lolesports.com）。

此 API 由 Riot 内部使用，通过 lolesports.com 前端 JS 逆向得到。
无需用户注册，使用前端内嵌的固定 API Key。
Key 可能被 Riot 在不通知的情况下更换，此时从浏览器 DevTools Network 面板
找 lolesports.com XHR 请求里的 x-api-key 替换 lolesports_api_key 配置即可。

仅返回 LoL 数据，作为 PandaScore LoL 的辅助冗余源。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.lolesports_api")

LOLESPORTS_BASE = "https://esports-api.lolesports.com/persisted/gw"
# Riot 前端内嵌 key（不可用时在 lolesports.com DevTools Network 里更新 lolesports_api_key）
_DEFAULT_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class LolesportsApiProvider:
    """LoL Esports 半官方 API 适配器。"""

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
        self.source_id = "lolesports_api"
        self._api_key = cfg.lolesports_api_key or _DEFAULT_KEY
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if self.limiter is not None and not self.limiter.try_acquire():
            return self._last
        try:
            client = await self.proxy.get_httpx_client()
            headers = {"x-api-key": self._api_key}

            # 拉取当前赛程（含 completed/inProgress/unstarted 状态）
            resp = await client.get(
                f"{LOLESPORTS_BASE}/getSchedule",
                headers=headers,
                params={"hl": "en-US"},
            )
            resp.raise_for_status()
            events = (
                resp.json()
                .get("data", {})
                .get("schedule", {})
                .get("events", [])
            )

            updates: list[FixtureUpdate] = []
            for ev in events:
                u = self._parse_event(ev)
                if u:
                    updates.append(u)

            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("lolesports_api 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

    def _parse_event(self, ev: dict) -> FixtureUpdate | None:
        if ev.get("type") != "match":
            return None
        state = str(ev.get("state") or "")
        if state == "completed":
            st = FixtureStatus.FINAL
        elif state == "inProgress":
            st = FixtureStatus.LIVE
        elif state == "unstarted":
            st = FixtureStatus.SCHEDULED
        else:
            return None

        match = ev.get("match") or {}
        match_id = str(match.get("id") or "")
        teams = match.get("teams") or []
        if len(teams) < 2:
            return None

        home_name = str(teams[0].get("name") or teams[0].get("code") or "")
        away_name = str(teams[1].get("name") or teams[1].get("code") or "")
        if not home_name or not away_name:
            return None

        winner: str | None = None
        home_score: int | None = None
        away_score: int | None = None
        if st == FixtureStatus.FINAL:
            r0 = teams[0].get("result") or {}
            r1 = teams[1].get("result") or {}
            home_score = r0.get("gameWins")
            away_score = r1.get("gameWins")
            outcome0 = str(r0.get("outcome") or "")
            outcome1 = str(r1.get("outcome") or "")
            if outcome0 == "win":
                winner = "home"
            elif outcome1 == "win":
                winner = "away"
            elif home_score is not None and away_score is not None:
                if home_score > away_score:
                    winner = "home"
                elif away_score > home_score:
                    winner = "away"

        return FixtureUpdate(
            fixture_key=f"lol:lolesports:{match_id}",
            sport=SportType.LOL,
            source_id=self.source_id,
            home_team=home_name,
            away_team=away_name,
            status=st,
            home_score=int(home_score) if home_score is not None else None,
            away_score=int(away_score) if away_score is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league="lol",
            external_id=match_id,
            kickoff_time=_parse_dt(ev.get("startTime")),
        )
