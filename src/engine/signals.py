"""信号引擎：终局 + 盘口 → 触发下单。"""

from __future__ import annotations

import logging
from typing import Any

from src.config import AppConfig
from src.engine.kickoff_align import (
    final_allowed_for_market,
    kickoff_delta_sec,
    kickoffs_aligned,
    parse_market_kickoff,
    pick_market_by_kickoff,
)
from src.engine.ladder_executor import LadderExecutor
from src.logging_setup import log_event
from src.matcher.reverse_matcher import ReverseMatcher, normalize_team, teams_match
from src.pm.clob_ws import ClobOrderbookFeed, OrderbookSnapshot
from src.sports.aggregator import FinalEvent, FixtureAggregator
from src.sports.base import FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.signals")

# UMA 非终态
UMA_BLOCK = frozenset({"resolved", "disputed"})


class SignalEngine:
    """连接终局事件与下单执行。"""

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        matcher: ReverseMatcher,
        aggregator: FixtureAggregator,
        books: ClobOrderbookFeed,
        ladder: LadderExecutor,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.matcher = matcher
        self.aggregator = aggregator
        self.books = books
        self.ladder = ladder
        self._processing: set[str] = set()
        # 已“武装”的直播市场：market_id -> 该市场上下文（含两侧 token）。
        # 一旦比赛进入早进场阶段（如足球 80 分钟后）即武装，盘口高频回调里据此判断买入。
        self._armed: dict[str, dict[str, Any]] = {}
        # token_id -> market_id 反查，便于盘口回调快速定位
        self._armed_tokens: dict[str, str] = {}
        # 正在执行直播下单的市场，避免同一盘口多次回调重复下单
        self._live_processing: set[str] = set()

    def _record_skip(
        self,
        market_id: str,
        reason: str,
        detail: str = "",
        *,
        sport: str = "",
        team_a: str = "",
        team_b: str = "",
        price: float = 0.0,
        event_type: str = "skip",
    ) -> None:
        """写入错过记录并供 Dashboard 展示。"""
        if not market_id:
            return
        self.store.record_signal_event(
            market_id=market_id,
            event_type=event_type,
            reason=reason,
            detail=detail,
            sport=sport,
            team_a=team_a,
            team_b=team_b,
            price=price,
        )

    def _emit_armed(self, market_id: str, armed: bool) -> None:
        from src.dashboard.bus import emit_event

        emit_event("watchlist.armed", {"market_id": market_id, "armed": armed})
        emit_event("status.updated", {})

    def _base_ctx(self, row: Any, ev: FinalEvent) -> dict[str, Any]:
        """策略日志公共字段。"""
        return {
            "market_id": row["market_id"],
            "question": row["question"],
            "sport": row["sport"],
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "match_key": ev.match_key,
            "home_team": ev.home_team,
            "away_team": ev.away_team,
            "winner": ev.winner,
            "home_score": ev.home_score,
            "away_score": ev.away_score,
            "source_id": ev.source_id,
            "entry_max_price": self.cfg.entry_max_price,
            "mode": self.cfg.mode,
        }

    def _book_ctx(self, book: OrderbookSnapshot) -> dict[str, Any]:
        """订单簿快照字段。"""
        return {
            "best_ask": book.best_ask,
            "best_ask_size": book.best_ask_size,
            "depth_usd": round(book.available_notional(self.cfg.entry_max_price), 4),
        }

    async def on_final(self, ev: FinalEvent) -> None:
        """终局回调。"""
        if self.aggregator.is_conflicted(ev.match_key):
            log_event(
                logger,
                "STRATEGY_SKIP",
                reason="result_conflict",
                match_key=ev.match_key,
                home_team=ev.home_team,
                away_team=ev.away_team,
                winner=ev.winner,
                source_id=ev.source_id,
            )
            return

        market_id = self._resolve_market_for_final(ev)
        if not market_id:
            return

        if market_id in self._processing:
            return
        self._processing.add(market_id)

        try:
            await self._try_execute(market_id, ev)
        finally:
            self._processing.discard(market_id)

    def _resolve_market_for_final(self, ev: FinalEvent) -> str | None:
        """终局事件 → 市场 id：先 match_key，再校验开球，否则按队名+开球重选。"""
        candidates: list[str] = []
        mid = self.matcher.market_id_for_final(ev.match_key)
        if mid:
            candidates.append(mid)
        matched = self._match_market(
            ev.sport.value, ev.home_team, ev.away_team, ev.kickoff_time
        )
        if matched and matched not in candidates:
            candidates.append(matched)

        for market_id in candidates:
            row = self.store.get_market(market_id)
            ok, reason = final_allowed_for_market(
                row,
                fixture_kickoff=ev.kickoff_time,
                observed_at=ev.observed_at,
            )
            if ok:
                return market_id
            log_event(
                logger,
                "STRATEGY_SKIP",
                reason=reason,
                market_id=market_id,
                match_key=ev.match_key,
                home_team=ev.home_team,
                away_team=ev.away_team,
                game_start_time=row["game_start_time"] if row else None,
                fixture_kickoff=ev.kickoff_time.isoformat() if ev.kickoff_time else None,
            )
        return None

    def _match_market(
        self,
        sport_value: str,
        home: str,
        away: str,
        kickoff: Any = None,
    ) -> str | None:
        """按运动+队名在 watchlist 中反查市场 id；多场同名时按开球时间择优。"""
        rows = self.store.list_active_watchlist()
        sport = "nba" if sport_value == "nba" else "football"
        candidates = []
        for row in rows:
            if row["sport"] != sport:
                continue
            ta = normalize_team(str(row["team_a"] or ""), self.store, sport)
            tb = normalize_team(str(row["team_b"] or ""), self.store, sport)
            eh = normalize_team(home, self.store, sport)
            ea = normalize_team(away, self.store, sport)
            if teams_match(ta, tb, eh, ea):
                candidates.append(row)
        picked = pick_market_by_kickoff(candidates, kickoff)
        return picked["market_id"] if picked else None

    # ---------------------------------------------------------------------
    # 价格驱动早进场（直播）：不等 final，比赛后段一旦某一方价格突破阈值即买入
    # ---------------------------------------------------------------------
    def arm_live_from_fixtures(self, fixtures: list[FixtureUpdate]) -> None:
        """根据当前各场实时状态，把已进入早进场阶段的比赛对应市场“武装”。

        足球：开赛 80 分钟后（或墙钟兜底）可直播价买入。
        NBA：默认不武装，仅终局赛果触发 on_final 下单。
        """
        if not self.cfg.early_entry_enabled:
            return
        for f in fixtures:
            if not self.aggregator.is_eligible_for_early_entry(f):
                continue
            market_id = self._match_market(
                f.sport.value, f.home_team, f.away_team, f.kickoff_time
            )
            if not market_id or market_id in self._armed:
                continue
            self._arm_market(market_id, f)

    def has_armed(self) -> bool:
        """是否存在已武装的直播市场（供主循环决定轮询节奏）。"""
        return bool(self._armed)

    def _arm_market(self, market_id: str, f: FixtureUpdate) -> None:
        row = self.store.get_market(market_id)
        if not row:
            return
        if row["closed"]:
            return
        if (row["watch_state"] or "") == "done":
            return
        uma = (row["uma_status"] or "").lower()
        if uma in UMA_BLOCK:
            return
        # 武装前校验开球，避免旧 LIVE 场次污染未来同名盘
        market_ko = parse_market_kickoff(row)
        if market_ko and f.kickoff_time and not kickoffs_aligned(market_ko, f.kickoff_time):
            return
        token_yes = row["token_yes"]
        token_no = row["token_no"]
        if not token_yes or not token_no:
            return
        self._armed[market_id] = {
            "market_id": market_id,
            "question": row["question"],
            "sport": row["sport"],
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "token_yes": token_yes,
            "token_no": token_no,
        }
        self._armed_tokens[token_yes] = market_id
        self._armed_tokens[token_no] = market_id
        self.books.subscribe(token_yes)
        self.books.subscribe(token_no)
        self._emit_armed(market_id, True)
        log_event(
            logger,
            "STRATEGY_LIVE_ARM",
            market_id=market_id,
            question=row["question"],
            sport=row["sport"],
            team_a=row["team_a"],
            team_b=row["team_b"],
            match_minute=f.match_minute_estimate(),
            elapsed_minute=f.elapsed_minute,
            period=f.period,
            home_team=f.home_team,
            away_team=f.away_team,
            home_score=f.home_score,
            away_score=f.away_score,
            source_id=f.source_id,
            early_entry_price=self.cfg.early_entry_price,
            entry_max_price=self.cfg.entry_max_price,
        )

    def _disarm(self, market_id: str) -> None:
        info = self._armed.pop(market_id, None)
        if not info:
            return
        for tk in (info.get("token_yes"), info.get("token_no")):
            if tk and self._armed_tokens.get(tk) == market_id:
                self._armed_tokens.pop(tk, None)
                self.books.unsubscribe(tk)
        self._emit_armed(market_id, False)

    async def on_book_update(self, token_id: str, snap: OrderbookSnapshot) -> None:
        """盘口更新回调：直播阶段一旦价格落入买入窗口立即下单。"""
        if not self.cfg.early_entry_enabled:
            return
        market_id = self._armed_tokens.get(token_id)
        if not market_id:
            return
        info = self._armed.get(market_id)
        # NBA 默认终局下单，防止历史 armed 状态误触发直播单
        if info and info.get("sport") == "nba" and not self.cfg.nba_early_entry_enabled:
            return
        if snap.best_ask is None:
            return
        # 买入窗口：[early_entry_price, entry_max_price]（默认 0.60~0.99）
        if snap.best_ask < self.cfg.early_entry_price:
            return
        if snap.best_ask > self.cfg.entry_max_price:
            return
        # CLOB 不接受 <0.01 的限价；0.001 多为输家 token，直接跳过
        if snap.best_ask is not None and snap.best_ask < 0.01:
            return
        if market_id in self._live_processing:
            return
        row = self.store.get_market(market_id)
        if row and not self._token_is_leading_side(row, token_id):
            return
        self._live_processing.add(market_id)
        try:
            await self._execute_live(market_id, token_id, snap)
        finally:
            self._live_processing.discard(market_id)

    def _live_fixture_for_market(self, row: Any) -> FixtureUpdate | None:
        """按 PM 对阵在聚合器实时快照中找当前比分。"""
        sport = SportType.NBA if row["sport"] == "nba" else SportType.FOOTBALL
        ta = str(row["team_a"] or "")
        tb = str(row["team_b"] or "")
        sp = row["sport"]
        market_ko = parse_market_kickoff(row)
        matched: list[FixtureUpdate] = []
        for f in self.aggregator.live_fixtures():
            if f.sport != sport:
                continue
            if teams_match(
                normalize_team(ta, self.store, sp),
                normalize_team(tb, self.store, sp),
                normalize_team(f.home_team, self.store, sp),
                normalize_team(f.away_team, self.store, sp),
            ):
                matched.append(f)
        if not matched:
            return None
        if len(matched) == 1:
            return matched[0]
        if market_ko is None:
            return matched[0]
        best: FixtureUpdate | None = None
        best_delta: float | None = None
        for f in matched:
            delta = kickoff_delta_sec(market_ko, f.kickoff_time)
            if delta is None:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = f
        return best

    def _token_is_leading_side(self, row: Any, token_id: str) -> bool:
        """直播早进场只买领先方 token，避免买 0.001 的输家侧。"""
        live = self._live_fixture_for_market(row)
        if live is None or live.home_score is None or live.away_score is None:
            return True
        if live.home_score == live.away_score:
            return True  # 平局时不拦，由盘口价格过滤
        leader = "home" if live.home_score > live.away_score else "away"
        side = self.matcher.winner_token_side(
            row, leader, home_team=live.home_team, away_team=live.away_team
        )
        if not side:
            return False
        expected = row["token_yes"] if side == "yes" else row["token_no"]
        return token_id == expected

    async def _execute_live(self, market_id: str, token_id: str, snap: OrderbookSnapshot) -> None:
        row = self.store.get_market(market_id)
        if not row or row["closed"] or (row["watch_state"] or "") == "done":
            self._disarm(market_id)
            return
        uma = (row["uma_status"] or "").lower()
        if uma in UMA_BLOCK:
            self._disarm(market_id)
            return

        side = "yes" if token_id == row["token_yes"] else "no"
        ctx: dict[str, Any] = {
            "market_id": market_id,
            "question": row["question"],
            "sport": row["sport"],
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "token_side": side,
            "token_id": token_id,
            "best_ask": snap.best_ask,
            "best_ask_size": snap.best_ask_size,
            "early_entry_price": self.cfg.early_entry_price,
            "entry_max_price": self.cfg.entry_max_price,
            "trigger": "live_price",
            "mode": self.cfg.mode,
        }
        log_event(
            logger,
            "STRATEGY_LIVE_SIGNAL",
            detail=f"直播价格进入 [{self.cfg.early_entry_price}, {self.cfg.entry_max_price}]，未等 final 即买入",
            **ctx,
        )

        result = await self.ladder.execute_buy(market_id, token_id, snap, strategy_ctx=ctx)
        if result.success:
            self.store.set_watch_state(market_id, "done", side)
            self._disarm(market_id)
            log_event(
                logger,
                "STRATEGY_ORDER",
                outcome="filled",
                filled_usd=result.filled_usd,
                price=result.price,
                status=result.status,
                detail=result.detail,
                **ctx,
            )
        else:
            self._record_skip(
                market_id,
                result.status,
                result.detail or "",
                sport=str(ctx.get("sport", "")),
                team_a=str(ctx.get("team_a", "")),
                team_b=str(ctx.get("team_b", "")),
                price=float(result.price or 0),
                event_type="order_not_filled",
            )
            log_event(
                logger,
                "STRATEGY_ORDER",
                outcome="not_filled",
                status=result.status,
                detail=result.detail,
                price=result.price,
                **ctx,
            )

    async def _try_execute(self, market_id: str, ev: FinalEvent) -> None:
        row = self.store.get_market(market_id)
        if not row:
            log_event(
                logger,
                "STRATEGY_SKIP",
                reason="market_not_in_db",
                market_id=market_id,
                match_key=ev.match_key,
            )
            return

        ctx = self._base_ctx(row, ev)
        log_event(logger, "STRATEGY_EVAL", action="final_received", **ctx)

        ok, kickoff_reason = final_allowed_for_market(
            row,
            fixture_kickoff=ev.kickoff_time,
            observed_at=ev.observed_at,
        )
        if not ok:
            log_event(
                logger,
                "STRATEGY_SKIP",
                reason=kickoff_reason,
                game_start_time=row["game_start_time"],
                fixture_kickoff=ev.kickoff_time.isoformat() if ev.kickoff_time else None,
                **ctx,
            )
            self._record_skip(
                market_id,
                kickoff_reason,
                str(row["game_start_time"] or ""),
                sport=ctx["sport"],
                team_a=str(ctx["team_a"]),
                team_b=str(ctx["team_b"]),
            )
            return

        uma = (row["uma_status"] or "").lower()
        if uma in UMA_BLOCK:
            log_event(logger, "STRATEGY_SKIP", reason="uma_resolved_or_disputed", uma_status=uma, **ctx)
            self._record_skip(
                market_id, "uma_resolved_or_disputed", uma or "", sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
            )
            return
        if row["closed"]:
            log_event(logger, "STRATEGY_SKIP", reason="market_closed", **ctx)
            self._record_skip(
                market_id, "market_closed", "", sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
            )
            return

        side = self.matcher.winner_token_side(
            row, ev.winner, home_team=ev.home_team, away_team=ev.away_team
        )
        if not side:
            # ctx 已含 winner，勿重复传参（否则会触发 log_event TypeError）
            log_event(logger, "STRATEGY_SKIP", reason="draw_or_unknown_winner_token", **ctx)
            self._record_skip(
                market_id, "draw_or_unknown_winner_token", "", sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
            )
            return

        token_id = row["token_yes"] if side == "yes" else row["token_no"]
        ctx["token_side"] = side
        ctx["token_id"] = token_id
        self.books.subscribe(token_id)

        try:
            book = await self.books.fetch_book_rest(token_id)
        except Exception as e:
            log_event(logger, "STRATEGY_SKIP", reason="orderbook_fetch_failed", error=str(e), **ctx)
            self._record_skip(
                market_id, "orderbook_fetch_failed", str(e), sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
            )
            return

        book_fields = self._book_ctx(book)
        ctx.update(book_fields)

        if book.best_ask is None:
            log_event(
                logger,
                "STRATEGY_NO_EDGE",
                reason="no_ask",
                detail="订单簿无卖单",
                **ctx,
            )
            self._record_skip(
                market_id, "no_ask", "订单簿无卖单", sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
                price=float(book.best_ask or 0),
            )
            return

        if book.best_ask < 0.01:
            log_event(
                logger,
                "STRATEGY_NO_EDGE",
                reason="ask_too_low",
                detail=f"ask={book.best_ask} 低于 CLOB 最小价 0.01，疑似输家 token",
                **ctx,
            )
            self._record_skip(
                market_id, "ask_too_low", ctx.get("detail", ""), sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
                price=float(book.best_ask or 0), event_type="skip",
            )
            return

        if book.best_ask > self.cfg.entry_max_price:
            log_event(
                logger,
                "STRATEGY_NO_EDGE",
                reason="ask_above_max",
                detail=f"ask={book.best_ask} > max={self.cfg.entry_max_price}，未下单",
                **ctx,
            )
            self._record_skip(
                market_id, "ask_above_max", f"ask={book.best_ask}", sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
                price=float(book.best_ask or 0), event_type="skip",
            )
            return

        if book.best_ask < self.cfg.early_entry_price:
            log_event(
                logger,
                "STRATEGY_NO_EDGE",
                reason="ask_below_min",
                detail=f"ask={book.best_ask} < min={self.cfg.early_entry_price}，未下单",
                **ctx,
            )
            self._record_skip(
                market_id, "ask_below_min", f"ask={book.best_ask}", sport=ctx["sport"],
                team_a=str(ctx["team_a"]), team_b=str(ctx["team_b"]),
                price=float(book.best_ask or 0), event_type="skip",
            )
            return

        log_event(logger, "STRATEGY_SIGNAL", detail="价格满足条件，开始分级下单", **ctx)

        result = await self.ladder.execute_buy(market_id, token_id, book, strategy_ctx=ctx)
        if result.success:
            self.store.set_watch_state(market_id, "done", ev.winner)
            self._disarm(market_id)
            log_event(
                logger,
                "STRATEGY_ORDER",
                outcome="filled",
                filled_usd=result.filled_usd,
                price=result.price,
                status=result.status,
                detail=result.detail,
                **ctx,
            )
        else:
            self._record_skip(
                market_id,
                result.status,
                result.detail or "",
                sport=str(ctx.get("sport", "")),
                team_a=str(ctx.get("team_a", "")),
                team_b=str(ctx.get("team_b", "")),
                price=float(result.price or 0),
                event_type="order_not_filled",
            )
            log_event(
                logger,
                "STRATEGY_ORDER",
                outcome="not_filled",
                status=result.status,
                detail=result.detail,
                price=result.price,
                **ctx,
            )
