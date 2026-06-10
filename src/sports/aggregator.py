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


def _pick_elapsed_minute(prev: FixtureUpdate | None, u: FixtureUpdate) -> int | None:
    """多源合并时优先保留 API-Football 的精确比赛分钟（含补时）。"""
    if u.source_id == "api_football" and u.elapsed_minute is not None:
        return u.elapsed_minute
    if prev and prev.source_id == "api_football" and prev.elapsed_minute is not None:
        return prev.elapsed_minute
    if u.elapsed_minute is not None:
        return u.elapsed_minute
    if prev:
        return prev.elapsed_minute
    return None


def _pick_kickoff(prev: FixtureUpdate | None, u: FixtureUpdate) -> datetime | None:
    """开球时间优先采用 API-Football（与 PM game_start_time 对齐更准）。"""
    if u.source_id == "api_football" and u.kickoff_time is not None:
        return u.kickoff_time
    if prev and prev.source_id == "api_football" and prev.kickoff_time is not None:
        return prev.kickoff_time
    return u.kickoff_time or (prev.kickoff_time if prev else None)


def _merge_live_updates(prev: FixtureUpdate, u: FixtureUpdate) -> FixtureUpdate:
    """合并两场更新：比分/状态取较新观测；分钟与开球保留 API-Football 精度。"""
    if u.observed_at < prev.observed_at:
        return prev

    # 终局状态优先
    if u.status == FixtureStatus.FINAL or prev.status == FixtureStatus.FINAL:
        status = FixtureStatus.FINAL
        winner = u.winner if u.status == FixtureStatus.FINAL else prev.winner
    elif u.status == FixtureStatus.LIVE or prev.status == FixtureStatus.LIVE:
        status = FixtureStatus.LIVE
        winner = None
    else:
        status = u.status
        winner = u.winner

    home_score = u.home_score if u.home_score is not None else prev.home_score
    away_score = u.away_score if u.away_score is not None else prev.away_score

    elapsed = _pick_elapsed_minute(prev, u)
    kickoff = _pick_kickoff(prev, u)

    # 分钟数以 API-Football 为准时，Dashboard 来源标签跟分钟源一致
    minute_src = u if (u.source_id == "api_football" and u.elapsed_minute is not None) else (
        prev if (prev.source_id == "api_football" and prev.elapsed_minute is not None) else u
    )

    return FixtureUpdate(
        fixture_key=u.fixture_key or prev.fixture_key,
        sport=u.sport,
        source_id=minute_src.source_id,
        home_team=u.home_team or prev.home_team,
        away_team=u.away_team or prev.away_team,
        status=status,
        home_score=home_score,
        away_score=away_score,
        winner=winner,
        observed_at=max(u.observed_at, prev.observed_at),
        league=u.league or prev.league,
        external_id=u.external_id or prev.external_id,
        elapsed_minute=elapsed,
        period=u.period if u.period is not None else prev.period,
        kickoff_time=kickoff,
    )


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

        - 足球（方案 B）：净胜球 ≥ football_blowout_lead 且已踢满 football_min_elapsed_min
          才可直播买；一球领先任意时刻均不可直播（仅终局 on_final）。
        - NBA：默认不参与早进场，仅终局(FINAL) 走 on_final 下单；若开启 nba_early_entry_enabled 则第四节后可武装。
        """
        if f.status == FixtureStatus.FINAL:
            return False
        if f.status != FixtureStatus.LIVE:
            return False
        if f.sport == SportType.FOOTBALL:
            if not self.cfg.football_early_entry_enabled:
                return False
            from src.engine.football_live_entry import is_football_live_entry_eligible

            return is_football_live_entry_eligible(f, self.cfg)
        if f.sport == SportType.NBA:
            # NBA 默认终局才下单，避免第四节未结束时误触发
            if not self.cfg.nba_early_entry_enabled:
                return False
            if f.period is not None:
                return f.period >= self.cfg.nba_min_period
            return False
        # MLB / NHL / NFL / CS2 / LOL：仅终局赛果触发下单，不走直播早进场
        return False

    def ingest(self, updates: list[FixtureUpdate]) -> list[FinalEvent]:
        """摄入更新，返回新触发的终局事件。"""
        new_events: list[FinalEvent] = []

        def _ingest_priority(u: FixtureUpdate) -> int:
            # API-Football 终局优先入账，减少 ESPN 等慢源抢先触发
            if (
                u.status == FixtureStatus.FINAL
                and u.winner
                and u.source_id == "api_football"
            ):
                return 0
            return 1

        for u in sorted(updates, key=_ingest_priority):
            # 记录最新实时状态（不限于终局），供早进场资格判断
            key0 = _match_key(u)
            prev = self._live.get(key0)
            if prev is None:
                merged = u
            elif u.observed_at >= prev.observed_at:
                merged = _merge_live_updates(prev, u)
            else:
                merged = None
            if merged is not None:
                self._live[key0] = merged
                from src.dashboard.bus import emit_event

                emit_event(
                    "fixture.updated",
                    {
                        "match_key": key0,
                        "fixture": {
                            "home_team": merged.home_team,
                            "away_team": merged.away_team,
                            "home_score": merged.home_score,
                            "away_score": merged.away_score,
                            "status": merged.status.value,
                            "elapsed_minute": merged.elapsed_minute,
                            "period": merged.period,
                            "source_id": merged.source_id,
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
