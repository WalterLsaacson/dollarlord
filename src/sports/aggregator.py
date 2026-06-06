"""多源赛果聚合：最快终局 + 冲突检测。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

from src.config import AppConfig
from src.sports.base import FixtureStatus, FixtureUpdate, SportType

logger = logging.getLogger("arb.aggregator")


def _match_key(u: FixtureUpdate) -> str:
    """按队名+运动类型归并（不同源 fixture_key 可能不同）。"""
    return f"{u.sport.value}:{u.normalized_home()}:{u.normalized_away()}"


@dataclass
class FinalEvent:
    """终局事件（触发交易信号）。"""

    match_key: str
    sport: SportType
    home_team: str
    away_team: str
    winner: str  # home | away | draw
    source_id: str
    observed_at: datetime
    home_score: int | None
    away_score: int | None
    # 赛果源报告的开球时间，用于与 PM game_start_time 对齐
    kickoff_time: datetime | None = None


@dataclass
class _MatchState:
    first_final: FinalEvent | None = None
    confirmations: list[tuple[str, str, datetime]] = field(default_factory=list)
    conflict: bool = False


class FixtureAggregator:
    """聚合多数据源更新。"""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._states: dict[str, _MatchState] = {}
        self._callbacks: list[Callable[[FinalEvent], Awaitable[None]]] = []
        # 每场比赛最新的实时状态（按 match_key 取 observed_at 最新者），
        # 用于价格驱动早进场时判断比赛进度（如足球是否已过 80 分钟）。
        self._live: dict[str, FixtureUpdate] = {}

    def on_final(self, cb: Callable[[FinalEvent], Awaitable[None]]) -> None:
        self._callbacks.append(cb)

    def live_fixtures(self) -> list[FixtureUpdate]:
        """返回各场比赛的最新实时状态快照。"""
        return list(self._live.values())

    def is_eligible_for_early_entry(self, f: FixtureUpdate) -> bool:
        """判断该场比赛是否已进入“可早进场”阶段。

        - 足球：进行中且开赛 80 分钟后（优先真实比赛分钟，无则墙钟兜底）。
        - NBA：默认不参与早进场，仅终局(FINAL) 走 on_final 下单；若开启 nba_early_entry_enabled 则第四节后可武装。
        """
        if f.status == FixtureStatus.FINAL:
            return False
        if f.status != FixtureStatus.LIVE:
            return False
        if f.sport == SportType.FOOTBALL:
            if f.elapsed_minute is not None:
                return f.elapsed_minute >= self.cfg.football_min_elapsed_min
            wc = f.match_minute_estimate()
            if wc is not None:
                return wc >= self.cfg.football_fallback_wallclock_min
            return False
        if f.sport == SportType.NBA:
            # NBA 默认终局才下单，避免第四节未结束时误触发
            if not self.cfg.nba_early_entry_enabled:
                return False
            if f.period is not None:
                return f.period >= self.cfg.nba_min_period
            return False
        return False

    def ingest(self, updates: list[FixtureUpdate]) -> list[FinalEvent]:
        """摄入更新，返回新触发的终局事件。"""
        new_events: list[FinalEvent] = []
        for u in updates:
            # 记录最新实时状态（不限于终局），供早进场资格判断
            key0 = _match_key(u)
            prev = self._live.get(key0)
            if prev is None or u.observed_at >= prev.observed_at:
                self._live[key0] = u
                from src.dashboard.bus import emit_event

                emit_event(
                    "fixture.updated",
                    {
                        "match_key": key0,
                        "fixture": {
                            "home_team": u.home_team,
                            "away_team": u.away_team,
                            "home_score": u.home_score,
                            "away_score": u.away_score,
                            "status": u.status.value,
                            "elapsed_minute": u.elapsed_minute,
                            "period": u.period,
                        },
                    },
                )

            if u.status != FixtureStatus.FINAL or not u.winner:
                continue
            key = _match_key(u)
            state = self._states.setdefault(key, _MatchState())

            if state.conflict:
                continue

            if state.first_final:
                # 确认或冲突
                existing_winner = state.first_final.winner
                if u.winner != existing_winner:
                    dt = (u.observed_at - state.first_final.observed_at).total_seconds()
                    if dt <= self.cfg.conflict_window_sec:
                        state.conflict = True
                        logger.warning(
                            "赛果冲突 %s: %s=%s vs %s=%s",
                            key,
                            state.first_final.source_id,
                            existing_winner,
                            u.source_id,
                            u.winner,
                        )
                continue

            ev = FinalEvent(
                match_key=key,
                sport=u.sport,
                home_team=u.home_team,
                away_team=u.away_team,
                winner=u.winner,
                source_id=u.source_id,
                observed_at=u.observed_at,
                home_score=u.home_score,
                away_score=u.away_score,
                kickoff_time=u.kickoff_time,
            )
            state.first_final = ev
            new_events.append(ev)
            logger.info(
                "FINAL %s %s vs %s winner=%s source=%s",
                key,
                u.home_team,
                u.away_team,
                u.winner,
                u.source_id,
            )
        return new_events

    async def emit(self, events: list[FinalEvent]) -> None:
        for ev in events:
            for cb in self._callbacks:
                await cb(ev)

    def is_conflicted(self, match_key: str) -> bool:
        st = self._states.get(match_key)
        return bool(st and st.conflict)
