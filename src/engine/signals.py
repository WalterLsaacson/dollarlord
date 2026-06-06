"""信号引擎：终局 + 盘口 → 触发下单。"""

from __future__ import annotations

import logging
from typing import Any

from src.config import AppConfig
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

        market_id = self.matcher.market_id_for_final(ev.match_key)
        if not market_id:
            market_id = self._find_market_by_teams(ev)
        if not market_id:
            return

        if market_id in self._processing:
            return
        self._processing.add(market_id)

        try:
            await self._try_execute(market_id, ev)
        finally:
            self._processing.discard(market_id)

    def _find_market_by_teams(self, ev: FinalEvent) -> str | None:
        return self._match_market(ev.sport.value, ev.home_team, ev.away_team)

    def _match_market(self, sport_value: str, home: str, away: str) -> str | None:
        """按运动+队名在 watchlist 中反查市场 id。"""
        rows = self.store.list_active_watchlist()
        sport = "nba" if sport_value == "nba" else "football"
        for row in rows:
            if row["sport"] != sport:
                continue
            ta = normalize_team(str(row["team_a"] or ""), self.store, sport)
            tb = normalize_team(str(row["team_b"] or ""), self.store, sport)
            eh = normalize_team(home, self.store, sport)
            ea = normalize_team(away, self.store, sport)
            if teams_match(ta, tb, eh, ea):
                return row["market_id"]
        return None

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
            market_id = self._match_market(f.sport.value, f.home_team, f.away_team)
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
        for f in self.aggregator.live_fixtures():
            if f.sport != sport:
                continue
            from src.matcher.reverse_matcher import normalize_team, teams_match

            sp = row["sport"]
            if teams_match(
                normalize_team(ta, self.store, sp),
                normalize_team(tb, self.store, sp),
                normalize_team(f.home_team, self.store, sp),
                normalize_team(f.away_team, self.store, sp),
            ):
                return f
        return None

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

        uma = (row["uma_status"] or "").lower()
        if uma in UMA_BLOCK:
            log_event(logger, "STRATEGY_SKIP", reason="uma_resolved_or_disputed", uma_status=uma, **ctx)
            return
        if row["closed"]:
            log_event(logger, "STRATEGY_SKIP", reason="market_closed", **ctx)
            return

        side = self.matcher.winner_token_side(
            row, ev.winner, home_team=ev.home_team, away_team=ev.away_team
        )
        if not side:
            # ctx 已含 winner，勿重复传参（否则会触发 log_event TypeError）
            log_event(logger, "STRATEGY_SKIP", reason="draw_or_unknown_winner_token", **ctx)
            return

        token_id = row["token_yes"] if side == "yes" else row["token_no"]
        ctx["token_side"] = side
        ctx["token_id"] = token_id
        self.books.subscribe(token_id)

        try:
            book = await self.books.fetch_book_rest(token_id)
        except Exception as e:
            log_event(logger, "STRATEGY_SKIP", reason="orderbook_fetch_failed", error=str(e), **ctx)
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
            return

        if book.best_ask < 0.01:
            log_event(
                logger,
                "STRATEGY_NO_EDGE",
                reason="ask_too_low",
                detail=f"ask={book.best_ask} 低于 CLOB 最小价 0.01，疑似输家 token",
                **ctx,
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
            return

        if book.best_ask < self.cfg.early_entry_price:
            log_event(
                logger,
                "STRATEGY_NO_EDGE",
                reason="ask_below_min",
                detail=f"ask={book.best_ask} < min={self.cfg.early_entry_price}，未下单",
                **ctx,
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
            log_event(
                logger,
                "STRATEGY_ORDER",
                outcome="not_filled",
                status=result.status,
                detail=result.detail,
                price=result.price,
                **ctx,
            )
