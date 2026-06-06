"""Dashboard 状态聚合与 WebSocket 增量广播。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from src.sports.base import FixtureStatus, FixtureUpdate, SportType

# Dashboard Watchlist 每页条数
WATCHLIST_PAGE_SIZE = 10

# 健康检查展示名
SOURCE_LABELS: dict[str, str] = {
    "gamma": "Gamma API",
    "clob": "CLOB API",
    "geoblock": "Geoblock",
    "payment_api": "CLOB 支付 API",
    "espn_soccer": "ESPN 足球",
    "openligadb": "OpenLigaDB",
    "football_data": "football-data.org",
    "api_football": "API-Football",
    "thesportsdb": "TheSportsDB",
    "espn_nba": "ESPN NBA",
    "balldontlie": "BallDontLie",
}

def _parse_game_start(iso: str | None) -> float:
    """将 game_start_time 转为排序用时间戳。"""
    if not iso:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return float("inf")


def _fixture_to_dict(f: FixtureUpdate) -> dict[str, Any]:
    return {
        "match_key": f"{f.sport.value}:{f.normalized_home()}:{f.normalized_away()}",
        "home_team": f.home_team,
        "away_team": f.away_team,
        "home_score": f.home_score,
        "away_score": f.away_score,
        "status": f.status.value,
        "elapsed_minute": f.elapsed_minute,
        "period": f.period,
        "minute_estimate": f.match_minute_estimate(),
        "source_id": f.source_id,
    }


class DashboardHub:
    """订阅 DashboardBus，维护缓存并向 WebSocket 客户端推送增量。"""

    def __init__(self, app_ref: Any) -> None:
        self._app = app_ref
        self._clients: set[WebSocket] = set()
        self._started_at = time.time()
        # 盘口推送防抖：token_id -> 待推送 payload
        self._book_pending: dict[str, dict[str, Any]] = {}
        self._book_flush_task: asyncio.Task[None] | None = None
        self._health_cache: dict[str, dict[str, Any]] = {}
        self._payment_api: dict[str, Any] = {"ok": None, "detail": "", "last_ts": 0}
        self._watchlist_total = 0
        # Watchlist 富化缓存，避免翻页时重复全量计算
        self._watchlist_cache: list[dict[str, Any]] | None = None
        self._watchlist_cache_at: float = 0.0
        self._watchlist_cache_ttl = 3.0

    def invalidate_watchlist_cache(self) -> None:
        """watchlist 变更时清缓存。"""
        self._watchlist_cache = None
        self._watchlist_cache_at = 0.0

    async def handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Bus 消费者入口。"""
        if event_type == "source.health":
            payload = {**payload, "label": SOURCE_LABELS.get(payload.get("id", ""), payload.get("id", ""))}
            self._health_cache[payload["id"]] = payload
            await self.broadcast({"type": "health.update", "item": payload})
        elif event_type == "health.critical":
            for item in payload.get("items", []):
                self._health_cache[item["id"]] = item
            await self.broadcast({"type": "health.critical", "items": payload.get("items", [])})
        elif event_type == "payment.api":
            self._payment_api = payload
            await self.broadcast({"type": "payment.api", "data": payload})
        elif event_type == "risk.updated":
            await self.broadcast({"type": "risk.updated", "data": self.build_status()})
        elif event_type == "fixture.updated":
            patches = self._build_fixture_patches(payload)
            if patches:
                await self.broadcast({"type": "watchlist.patch", "items": patches})
            focus = self.build_focus()
            if focus:
                await self.broadcast({"type": "focus.updated", "data": focus})
        elif event_type == "watchlist.armed":
            await self.broadcast({"type": "watchlist.armed", "data": payload})
            await self._push_status()
            focus = self.build_focus()
            if focus:
                await self.broadcast({"type": "focus.updated", "data": focus})
        elif event_type == "book.updated":
            self._schedule_book_push(payload)
        elif event_type == "watchlist.changed":
            self.invalidate_watchlist_cache()
            page = payload.get("page", 1)
            data = self.build_watchlist_page(page, WATCHLIST_PAGE_SIZE)
            await self.broadcast({"type": "watchlist.page", "data": data})
        elif event_type == "history.new":
            await self.broadcast({"type": "history.new", "item": payload})
        elif event_type == "positions.changed":
            await self.broadcast({"type": "positions.changed", "data": {}})
        elif event_type == "status.updated":
            await self.broadcast({"type": "status.updated", "data": self.build_status()})
        elif event_type == "log.append":
            await self.broadcast({"type": "log.append", "line": payload})

    def _schedule_book_push(self, payload: dict[str, Any]) -> None:
        token_id = payload.get("token_id", "")
        if token_id:
            self._book_pending[token_id] = payload
        if self._book_flush_task is None or self._book_flush_task.done():
            self._book_flush_task = asyncio.create_task(self._flush_book_pending())

    async def _flush_book_pending(self) -> None:
        await asyncio.sleep(0.2)
        pending = dict(self._book_pending)
        self._book_pending.clear()
        patches = self._build_book_patches(pending)
        if patches:
            await self.broadcast({"type": "watchlist.patch", "items": patches})
        focus = self.build_focus()
        if focus:
            await self.broadcast({"type": "focus.updated", "data": focus})

    def _market_fixture(self, row: Any) -> FixtureUpdate | None:
        app = self._app
        sport = SportType.NBA if row["sport"] == "nba" else SportType.FOOTBALL
        ta = str(row["team_a"] or "")
        tb = str(row["team_b"] or "")
        from src.matcher.reverse_matcher import normalize_team, teams_match

        for f in app.aggregator.live_fixtures():
            if f.sport != sport:
                continue
            sp = row["sport"]
            if teams_match(
                normalize_team(ta, app.store, sp),
                normalize_team(tb, app.store, sp),
                normalize_team(f.home_team, app.store, sp),
                normalize_team(f.away_team, app.store, sp),
            ):
                return f
        return None

    def _enrich_market_row(self, row: Any) -> dict[str, Any]:
        app = self._app
        market_id = row["market_id"]
        armed = market_id in app.signals._armed
        fixture = self._market_fixture(row)
        book_yes = app.books.get_book(row["token_yes"]) if row["token_yes"] else None
        book_no = app.books.get_book(row["token_no"]) if row["token_no"] else None
        item: dict[str, Any] = {
            "market_id": market_id,
            "question": row["question"],
            "sport": row["sport"],
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "game_start_time": row["game_start_time"],
            "armed": armed,
            "yes_ask": book_yes.best_ask if book_yes else None,
            "no_ask": book_no.best_ask if book_no else None,
            "fixture": _fixture_to_dict(fixture) if fixture else None,
        }
        return item

    def _get_enriched_watchlist(self) -> list[dict[str, Any]]:
        """带 TTL 的 watchlist 富化缓存。"""
        now = time.monotonic()
        if (
            self._watchlist_cache is not None
            and now - self._watchlist_cache_at < self._watchlist_cache_ttl
        ):
            return self._watchlist_cache
        rows = self._app.store.list_future_watchlist()
        enriched = [self._enrich_market_row(r) for r in rows]
        enriched.sort(key=lambda x: _parse_game_start(x.get("game_start_time")))
        self._watchlist_cache = enriched
        self._watchlist_cache_at = now
        return enriched

    def build_watchlist_page(
        self, page: int = 1, page_size: int = WATCHLIST_PAGE_SIZE
    ) -> dict[str, Any]:
        enriched = self._get_enriched_watchlist()
        total = len(enriched)
        offset = max(0, (page - 1) * page_size)
        items = enriched[offset : offset + page_size]
        self._watchlist_total = total
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def build_focus(self) -> dict[str, Any] | None:
        """焦点比赛：当前最可能触发买入的监听场次（ARMED 优先，其次直播中，否则最近开赛）。"""
        rows = self._app.store.list_future_watchlist()
        if not rows:
            return None
        candidates: list[tuple[int, Any]] = []
        for row in rows:
            item = self._enrich_market_row(row)
            fixture = item.get("fixture")
            score = 0
            if item["armed"]:
                score += 100
            if fixture and fixture.get("status") == FixtureStatus.LIVE.value:
                score += 50
            start_ts = _parse_game_start(row["game_start_time"])
            candidates.append((score * 1e12 - start_ts, item))
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1] if candidates else None

    def _build_health_item(self, sid: str) -> dict[str, Any]:
        """组装单个健康检查卡片（含未配置 Key / 等待拉取状态）。"""
        label = SOURCE_LABELS.get(sid, sid)

        if sid == "payment_api":
            return {
                "id": sid,
                "ok": self._payment_api.get("ok"),
                "last_ts": self._payment_api.get("last_ts", 0),
                "error": self._payment_api.get("detail", ""),
                "label": label,
            }

        cached = self._health_cache.get(sid)
        if cached:
            return {**cached, "label": label}

        provider = getattr(self._app, sid, None)
        if provider is not None and hasattr(provider, "enabled") and not provider.enabled:
            return {
                "id": sid,
                "ok": None,
                "status": "disabled",
                "last_ts": 0,
                "error": "未配置 API Key，已跳过",
                "label": label,
            }

        row = next(
            (r for r in self._app.store.list_source_health() if r["source_id"] == sid),
            None,
        )
        if row:
            ok = bool(row["last_ok_ts"]) and not row["last_error"]
            return {
                "id": sid,
                "ok": ok,
                "status": "ok" if ok else "error",
                "last_ts": row["last_ok_ts"] or 0,
                "error": row["last_error"] or "",
                "label": label,
            }

        if sid in ("gamma", "clob", "geoblock"):
            return {
                "id": sid,
                "ok": None,
                "status": "pending",
                "last_ts": 0,
                "error": "等待健康检查",
                "label": label,
            }

        if provider is not None:
            return {
                "id": sid,
                "ok": None,
                "status": "pending",
                "last_ts": 0,
                "error": "等待首次拉取",
                "label": label,
            }

        return {"id": sid, "ok": None, "last_ts": 0, "error": "", "label": label}

    def build_health_panel(self) -> list[dict[str, Any]]:
        """合并 critical 检查项与 source_health。"""
        known_ids = [
            "gamma",
            "clob",
            "geoblock",
            "payment_api",
            "espn_soccer",
            "openligadb",
            "football_data",
            "api_football",
            "thesportsdb",
            "espn_nba",
            "balldontlie",
        ]
        return [self._build_health_item(sid) for sid in known_ids]

    def build_status(self) -> dict[str, Any]:
        app = self._app
        return {
            "pid": os.getpid(),
            "uptime_sec": int(time.time() - self._started_at),
            "mode": app.cfg.mode,
            "proxy_enabled": app.cfg.proxy.enabled,
            "geoblocked": app.risk.geoblocked,
            "live_paused": app.risk.live_paused,
            "armed_count": len(app.signals._armed),
            "watchlist_total": self._watchlist_total or len(self._app.store.list_future_watchlist()),
            "auto_redeem_enabled": app.cfg.auto_redeem_enabled,
            "redeem_enabled": app.redeem.enabled(),
        }

    def build_snapshot(self, watchlist_page: int = 1, history_page: int = 1) -> dict[str, Any]:
        history_items, history_total = self._app.store.list_merged_history(history_page, 5)
        return {
            "type": "snapshot.full",
            "health": self.build_health_panel(),
            "watchlist": self.build_watchlist_page(watchlist_page, WATCHLIST_PAGE_SIZE),
            "focus": self.build_focus(),
            "history": {
                "items": history_items,
                "total": history_total,
                "page": history_page,
                "page_size": 5,
            },
            "status": self.build_status(),
            "logs": self._tail_logs(100),
        }

    def _tail_logs(self, n: int) -> list[dict[str, Any]]:
        log_path = self._app.cfg.resolve_path(self._app.cfg.log_path)
        if not log_path.is_file():
            return []
        try:
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            out: list[dict[str, Any]] = []
            for line in lines[-n:]:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return out
        except Exception:
            return []

    def _build_fixture_patches(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        match_key = payload.get("match_key")
        if not match_key:
            return []
        patches: list[dict[str, Any]] = []
        for row in self._app.store.list_future_watchlist():
            item = self._enrich_market_row(row)
            fx = item.get("fixture")
            if fx and fx.get("match_key") == match_key:
                patches.append({"market_id": item["market_id"], "fixture": fx})
        return patches

    def _build_book_patches(self, pending: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        patches: list[dict[str, Any]] = []
        for row in self._app.store.list_future_watchlist():
            mid = row["market_id"]
            yes_t = row["token_yes"]
            no_t = row["token_no"]
            upd: dict[str, Any] = {"market_id": mid}
            changed = False
            if yes_t in pending:
                upd["yes_ask"] = pending[yes_t].get("best_ask")
                changed = True
            if no_t in pending:
                upd["no_ask"] = pending[no_t].get("best_ask")
                changed = True
            if changed:
                patches.append(upd)
        return patches

    async def register_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    async def unregister_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        text = json.dumps(msg, ensure_ascii=False, default=str)
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _push_status(self) -> None:
        await self.broadcast({"type": "status.updated", "data": self.build_status()})
