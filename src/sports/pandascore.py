"""PandaScore 电竞数据源（CS2 + LoL）。

免费注册即可使用，限额 1,000 次/小时。
CS2 和 LoL 两个实例共享同一个 AsyncRateLimiter，避免超额。

注册地址：https://pandascore.co
API Key 放入 polymarket-arb.env: PANDASCORE_KEY=your_token
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.pandascore")

PANDASCORE_BASE = "https://api.pandascore.co"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class PandascoreProvider:
    """PandaScore 适配器（单个运动实例，game='csgo' 或 'lol'）。"""

    def __init__(
        self,
        cfg: AppConfig,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter,
        game: str,
        sport_type: SportType,
    ) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.game = game          # "csgo" or "lol"
        self.sport_type = sport_type
        self.source_id = f"pandascore_{game}"
        self.enabled = bool(cfg.pandascore_key)
        self._last: list[FixtureUpdate] = []

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if not self.enabled:
            return []
        if not self.limiter.try_acquire():
            return self._last
        try:
            updates: list[FixtureUpdate] = []
            seen: set[int] = set()
            client = await self.proxy.get_httpx_client()
            headers = {"Authorization": f"Bearer {self.cfg.pandascore_key}"}

            # 正在进行的比赛（LIVE 状态）
            resp = await client.get(
                f"{PANDASCORE_BASE}/{self.game}/matches/running",
                headers=headers,
                params={"page[size]": "20"},
            )
            resp.raise_for_status()
            for match in resp.json():
                u = self._parse_match(match, FixtureStatus.LIVE)
                if u and u.external_id:
                    mid = int(u.external_id)
                    if mid not in seen:
                        seen.add(mid)
                        updates.append(u)

            # 最近完赛（过去 48 小时）
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            resp = await client.get(
                f"{PANDASCORE_BASE}/{self.game}/matches/past",
                headers=headers,
                params={
                    "sort": "-begin_at",
                    "page[size]": "30",
                    "range[begin_at]": f"{cutoff},{now_str}",
                },
            )
            resp.raise_for_status()
            for match in resp.json():
                u = self._parse_match(match, FixtureStatus.FINAL)
                if u and u.external_id:
                    mid = int(u.external_id)
                    if mid not in seen:
                        seen.add(mid)
                        updates.append(u)

            self.store.touch_source(self.source_id)
            self._last = updates
            return updates
        except Exception as e:
            logger.warning("pandascore_%s 拉取失败: %s", self.game, e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

    def _parse_match(
        self, match: dict[str, Any], status_hint: FixtureStatus
    ) -> FixtureUpdate | None:
        match_id = match.get("id")
        if not match_id:
            return None

        opponents = match.get("opponents") or []
        if len(opponents) < 2:
            return None
        team_a_info = (opponents[0].get("opponent") or {})
        team_b_info = (opponents[1].get("opponent") or {})
        home_name = str(team_a_info.get("name") or "")
        away_name = str(team_b_info.get("name") or "")
        if not home_name or not away_name:
            return None

        # PandaScore 用 "home"/"away" 并不适用于电竞，用 opponents 顺序即可
        status_raw = str(match.get("status") or "")
        if status_raw == "finished":
            st = FixtureStatus.FINAL
        elif status_raw == "running":
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        winner_info = match.get("winner") or {}
        winner_id = winner_info.get("id")
        winner: str | None = None
        if st == FixtureStatus.FINAL and winner_id:
            if winner_id == team_a_info.get("id"):
                winner = "home"
            elif winner_id == team_b_info.get("id"):
                winner = "away"

        results = match.get("results") or []
        home_score: int | None = None
        away_score: int | None = None
        for r in results:
            tid = r.get("team_id")
            score = r.get("score")
            if tid == team_a_info.get("id"):
                home_score = int(score) if score is not None else None
            elif tid == team_b_info.get("id"):
                away_score = int(score) if score is not None else None

        league_name = (match.get("league") or {}).get("name") or self.game

        return FixtureUpdate(
            fixture_key=f"{self.sport_type.value}:pandascore:{match_id}",
            sport=self.sport_type,
            source_id=self.source_id,
            home_team=home_name,
            away_team=away_name,
            status=st,
            home_score=home_score,
            away_score=away_score,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=league_name,
            external_id=str(match_id),
            kickoff_time=_parse_dt(match.get("begin_at")),
        )
