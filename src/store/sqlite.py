"""SQLite 持久化：市场、映射、成交、cooldown。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 北京时间（UTC+8）
_BJ = timezone(timedelta(hours=8))


def format_ts_beijing(ts: float | int | None) -> str:
    """Unix 时间戳 → 北京时间字符串。"""
    if ts is None:
        return "—"
    try:
        sec = float(ts)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "—"
    dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(_BJ)
    return dt.strftime("%Y/%m/%d %H:%M:%S")


# 成交/错过面板不展示的高频噪音（仍写入 DB 供排查）
_HISTORY_NOISE = frozenset({
    ("risk_block", "cooldown"),
    ("risk_block", "live_paused"),
    ("risk_block", "skipped"),
    ("order_not_filled", "skipped"),
    ("skip", "orderbook_fetch_failed"),
})


def history_event_visible(event_type: str, reason: str) -> bool:
    """是否在 Dashboard 成交/错过列表展示。"""
    return (event_type, reason) not in _HISTORY_NOISE


@dataclass
class MarketRow:
    """PM 市场行。"""

    market_id: str
    condition_id: str
    question: str
    sport: str
    token_id_yes: str
    token_id_no: str
    outcome_names: list[str]
    game_start_time: str | None
    team_a: str | None
    team_b: str | None
    closed: bool
    uma_status: str | None
    slug: str | None
    watch_state: str  # unmapped | watching | done | conflict


class Store:
    """SQLite 存储封装。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _ensure_signal_events_table(self, cur: sqlite3.Cursor) -> None:
        """迁移：旧库补 signal_events 表。"""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                event_type TEXT,
                reason TEXT,
                detail TEXT,
                sport TEXT,
                team_a TEXT,
                team_b TEXT,
                price REAL,
                created_at REAL
            )
            """
        )
        self._conn.commit()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                condition_id TEXT,
                question TEXT,
                sport TEXT,
                token_yes TEXT,
                token_no TEXT,
                outcome_names TEXT,
                game_start_time TEXT,
                team_a TEXT,
                team_b TEXT,
                closed INTEGER DEFAULT 0,
                uma_status TEXT,
                slug TEXT,
                watch_state TEXT DEFAULT 'unmapped',
                fixture_key TEXT,
                matched_sources TEXT,
                winner_side TEXT,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS team_aliases (
                alias TEXT,
                canonical TEXT,
                sport TEXT,
                PRIMARY KEY (alias, sport)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                mode TEXT,
                notional_usd REAL,
                price REAL,
                status TEXT,
                detail TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS cooldowns (
                market_id TEXT PRIMARY KEY,
                until_ts REAL
            );
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY,
                last_ok_ts REAL,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                event_type TEXT,
                reason TEXT,
                detail TEXT,
                sport TEXT,
                team_a TEXT,
                team_b TEXT,
                price REAL,
                created_at REAL
            );
            """
        )
        self._conn.commit()
        self._ensure_signal_events_table(cur)
        self._ensure_redemptions_table(cur)

    def _ensure_redemptions_table(self, cur: sqlite3.Cursor) -> None:
        """迁移：结算记录表。"""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT,
                title TEXT,
                size REAL,
                cur_price REAL,
                usdc_gained REAL,
                tx_hash TEXT,
                status TEXT,
                detail TEXT,
                trigger TEXT,
                created_at REAL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_redemptions_condition ON redemptions(condition_id)"
        )
        self._conn.commit()

    def upsert_market(self, row: MarketRow, **extra: Any) -> None:
        """插入或更新市场。"""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO markets (
                market_id, condition_id, question, sport, token_yes, token_no,
                outcome_names, game_start_time, team_a, team_b, closed, uma_status,
                slug, watch_state, fixture_key, matched_sources, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                closed=excluded.closed,
                uma_status=excluded.uma_status,
                watch_state=excluded.watch_state,
                fixture_key=COALESCE(excluded.fixture_key, markets.fixture_key),
                matched_sources=COALESCE(excluded.matched_sources, markets.matched_sources),
                updated_at=excluded.updated_at
            """,
            (
                row.market_id,
                row.condition_id,
                row.question,
                row.sport,
                row.token_id_yes,
                row.token_id_no,
                json.dumps(row.outcome_names, ensure_ascii=False),
                row.game_start_time,
                row.team_a,
                row.team_b,
                int(row.closed),
                row.uma_status,
                row.slug,
                row.watch_state,
                extra.get("fixture_key"),
                extra.get("matched_sources"),
                time.time(),
            ),
        )
        self._conn.commit()

    def set_market_mapping(
        self,
        market_id: str,
        fixture_key: str,
        matched_sources: list[str],
        watch_state: str = "watching",
    ) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE markets SET fixture_key=?, matched_sources=?, watch_state=?, updated_at=?
            WHERE market_id=?
            """,
            (fixture_key, json.dumps(matched_sources), watch_state, time.time(), market_id),
        )
        self._conn.commit()

    def list_watching_markets(self) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM markets WHERE watch_state IN ('watching', 'unmapped') AND closed=0"
        )
        return cur.fetchall()

    def list_active_watchlist(self) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM markets WHERE watch_state='watching' AND closed=0")
        return cur.fetchall()

    def list_future_watchlist(self, grace_hours: float = 2.0) -> list[sqlite3.Row]:
        """Dashboard 用：所有 watching + 开赛后 grace_hours 内的 done。"""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=grace_hours)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT * FROM markets
            WHERE closed=0 AND (
                watch_state='watching'
                OR (
                    watch_state='done'
                    AND (game_start_time IS NULL OR game_start_time >= ?)
                )
            )
            ORDER BY game_start_time ASC
            """,
            (cutoff_iso,),
        )
        return cur.fetchall()

    def get_market_by_condition_id(self, condition_id: str) -> sqlite3.Row | None:
        """按 condition_id 查 PM 市场（持仓展示盘口标题）。"""
        if not condition_id:
            return None
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM markets WHERE lower(condition_id)=lower(?) LIMIT 1",
            (condition_id,),
        )
        return cur.fetchone()

    def get_market_by_token(self, token_id: str) -> sqlite3.Row | None:
        """按 yes/no outcome token id 反查市场。"""
        if not token_id:
            return None
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM markets WHERE token_yes=? OR token_no=? LIMIT 1",
            (token_id, token_id),
        )
        return cur.fetchone()

    def get_market(self, market_id: str) -> sqlite3.Row | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM markets WHERE market_id=?", (market_id,))
        return cur.fetchone()

    def list_traded_market_ids(self) -> set[str]:
        """已有成交记录的市场 id（Watchlist 排序用）。"""
        cur = self._conn.cursor()
        cur.execute("SELECT DISTINCT market_id FROM trades")
        return {str(r["market_id"]) for r in cur.fetchall()}

    def set_watch_state(self, market_id: str, state: str, winner_side: str | None = None) -> None:
        cur = self._conn.cursor()
        had_trade = False
        if state == "done":
            cur.execute("SELECT COUNT(*) AS c FROM trades WHERE market_id=?", (market_id,))
            had_trade = int(cur.fetchone()["c"]) > 0
        if winner_side:
            cur.execute(
                "UPDATE markets SET watch_state=?, winner_side=?, updated_at=? WHERE market_id=?",
                (state, winner_side, time.time(), market_id),
            )
        else:
            cur.execute(
                "UPDATE markets SET watch_state=?, updated_at=? WHERE market_id=?",
                (state, time.time(), market_id),
            )
        self._conn.commit()
        # 终局无成交 → 记入错过历史
        if state == "done" and not had_trade:
            row = self.get_market(market_id)
            if row:
                self.record_signal_event(
                    market_id=market_id,
                    event_type="done_no_trade",
                    reason="done_no_trade",
                    detail="比赛已结束但未成交",
                    sport=str(row["sport"] or ""),
                    team_a=str(row["team_a"] or ""),
                    team_b=str(row["team_b"] or ""),
                    price=0.0,
                )

    def add_team_alias(self, alias: str, canonical: str, sport: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO team_aliases (alias, canonical, sport) VALUES (?, ?, ?)",
            (alias.lower().strip(), canonical.lower().strip(), sport),
        )
        self._conn.commit()

    def resolve_alias(self, name: str, sport: str) -> str:
        key = name.lower().strip()
        cur = self._conn.cursor()
        cur.execute(
            "SELECT canonical FROM team_aliases WHERE alias=? AND sport=?",
            (key, sport),
        )
        row = cur.fetchone()
        return row["canonical"] if row else key

    def is_on_cooldown(self, market_id: str) -> bool:
        cur = self._conn.cursor()
        cur.execute("SELECT until_ts FROM cooldowns WHERE market_id=?", (market_id,))
        row = cur.fetchone()
        return bool(row and row["until_ts"] > time.time())

    def set_cooldown(self, market_id: str, seconds: int) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO cooldowns (market_id, until_ts) VALUES (?, ?)",
            (market_id, time.time() + seconds),
        )
        self._conn.commit()

    def record_trade(
        self,
        market_id: str,
        mode: str,
        notional_usd: float,
        price: float,
        status: str,
        detail: str = "",
    ) -> int:
        cur = self._conn.cursor()
        row = self.get_market(market_id)
        ts = time.time()
        cur.execute(
            """
            INSERT INTO trades (market_id, mode, notional_usd, price, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (market_id, mode, notional_usd, price, status, detail, ts),
        )
        self._conn.commit()
        trade_id = int(cur.lastrowid)
        from src.dashboard.bus import emit_event

        emit_event(
            "history.new",
            {
                "kind": "success",
                "id": trade_id,
                "market_id": market_id,
                "event_type": "trade",
                "reason": status,
                "detail": detail,
                "sport": str(row["sport"] if row else ""),
                "team_a": str(row["team_a"] if row else ""),
                "team_b": str(row["team_b"] if row else ""),
                "question": str(row["question"] if row else ""),
                "price": price,
                "notional_usd": notional_usd,
                "created_at": ts,
                "created_at_display": format_ts_beijing(ts),
            },
        )
        return trade_id

    def record_signal_event(
        self,
        *,
        market_id: str,
        event_type: str,
        reason: str,
        detail: str = "",
        sport: str = "",
        team_a: str = "",
        team_b: str = "",
        price: float = 0.0,
    ) -> int:
        """持久化策略跳过/未成交事件，并推送 dashboard。"""
        cur = self._conn.cursor()
        ts = time.time()
        cur.execute(
            """
            INSERT INTO signal_events
            (market_id, event_type, reason, detail, sport, team_a, team_b, price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (market_id, event_type, reason, detail, sport, team_a, team_b, price, ts),
        )
        self._conn.commit()
        event_id = int(cur.lastrowid)
        row = self.get_market(market_id)
        if history_event_visible(event_type, reason):
            from src.dashboard.bus import emit_event

            emit_event(
                "history.new",
                {
                    "kind": "missed",
                    "id": event_id,
                    "market_id": market_id,
                    "event_type": event_type,
                    "reason": reason,
                    "detail": detail,
                    "sport": sport or str(row["sport"] if row else ""),
                    "team_a": team_a or str(row["team_a"] if row else ""),
                    "team_b": team_b or str(row["team_b"] if row else ""),
                    "question": str(row["question"] if row else ""),
                    "price": price,
                    "created_at": ts,
                    "created_at_display": format_ts_beijing(ts),
                },
            )
        return event_id

    def is_condition_redeemed(self, condition_id: str) -> bool:
        """该 condition 是否已成功结算过（避免重复发链上交易）。"""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM redemptions
            WHERE condition_id=? AND status='success'
            LIMIT 1
            """,
            (condition_id,),
        )
        return cur.fetchone() is not None

    def record_redemption(
        self,
        *,
        condition_id: str,
        title: str,
        size: float,
        cur_price: float,
        tx_hash: str,
        status: str,
        detail: str = "",
        trigger: str = "manual",
        usdc_gained: float = 0.0,
    ) -> int:
        """写入结算记录并推送 Dashboard。"""
        cur = self._conn.cursor()
        ts = time.time()
        cur.execute(
            """
            INSERT INTO redemptions
            (condition_id, title, size, cur_price, usdc_gained, tx_hash, status, detail, trigger, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                condition_id,
                title,
                size,
                cur_price,
                usdc_gained,
                tx_hash,
                status,
                detail,
                trigger,
                ts,
            ),
        )
        self._conn.commit()
        rid = int(cur.lastrowid)
        from src.dashboard.bus import emit_event

        emit_event(
            "history.new",
            {
                "kind": "redeem",
                "id": rid,
                "market_id": "",
                "event_type": "redeem",
                "reason": status,
                "detail": detail or tx_hash,
                "sport": "",
                "team_a": title,
                "team_b": trigger,
                "question": title,
                "price": usdc_gained,
                "notional_usd": usdc_gained,
                "created_at": ts,
                "created_at_display": format_ts_beijing(ts),
            },
        )
        emit_event("positions.changed", {})
        return rid

    def list_trades(self, limit: int = 5, offset: int = 0) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT t.*, m.question, m.team_a, m.team_b, m.sport
            FROM trades t
            LEFT JOIN markets m ON m.market_id = t.market_id
            ORDER BY t.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return cur.fetchall()

    def count_trades(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM trades")
        return int(cur.fetchone()["c"])

    def list_signal_events(self, limit: int = 5, offset: int = 0) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT s.*, m.question
            FROM signal_events s
            LEFT JOIN markets m ON m.market_id = s.market_id
            ORDER BY s.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return cur.fetchall()

    def count_signal_events(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM signal_events")
        return int(cur.fetchone()["c"])

    def list_merged_history_all(self) -> list[dict]:
        """返回全部成交/错过/结算记录（无分页）。"""
        items, _ = self.list_merged_history(page=1, page_size=0)
        return items

    def list_merged_history(self, page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
        """合并成交与错过记录，SQL 分页；page_size=0 表示返回全部。"""
        cur = self._conn.cursor()
        skip_noise = """
            NOT (
                (s.event_type='risk_block' AND s.reason IN ('cooldown', 'live_paused', 'skipped'))
                OR (s.event_type='order_not_filled' AND s.reason='skipped')
                OR (s.event_type='skip' AND s.reason='orderbook_fetch_failed')
            )
        """
        cur.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT t.created_at FROM trades t
                UNION ALL
                SELECT s.created_at FROM signal_events s
                WHERE {skip_noise}
                UNION ALL
                SELECT r.created_at FROM redemptions r
            )
            """
        )
        total = int(cur.fetchone()["c"])
        offset = max(0, (page - 1) * page_size) if page_size > 0 else 0
        limit_sql = "LIMIT ? OFFSET ?" if page_size > 0 else ""
        params: tuple = (page_size, offset) if page_size > 0 else ()
        cur.execute(
            f"""
            SELECT kind, id, market_id, mode, notional_usd, price, reason, detail,
                   created_at, question, team_a, team_b, sport, event_type
            FROM (
                SELECT 'success' AS kind, t.id, t.market_id, t.mode, t.notional_usd, t.price,
                       t.status AS reason, t.detail, t.created_at,
                       m.question, m.team_a, m.team_b, m.sport, 'trade' AS event_type
                FROM trades t
                LEFT JOIN markets m ON m.market_id = t.market_id
                UNION ALL
                SELECT 'missed' AS kind, s.id, s.market_id, NULL, NULL, s.price,
                       s.reason, s.detail, s.created_at,
                       COALESCE(m.question, s.team_a), s.team_a, s.team_b, s.sport, s.event_type
                FROM signal_events s
                LEFT JOIN markets m ON m.market_id = s.market_id
                WHERE {skip_noise}
                UNION ALL
                SELECT 'redeem' AS kind, r.id, NULL, NULL, r.usdc_gained, r.cur_price,
                       r.status AS reason, r.detail, r.created_at,
                       r.title AS question, r.title AS team_a, r.trigger AS team_b,
                       '' AS sport, 'redeem' AS event_type
                FROM redemptions r
            )
            ORDER BY created_at DESC
            {limit_sql}
            """,
            params,
        )
        items: list[dict] = []
        for r in cur.fetchall():
            item = dict(r)
            item["created_at_display"] = format_ts_beijing(item.get("created_at"))
            items.append(item)
        return items, total

    def count_trades_today(self) -> int:
        cur = self._conn.cursor()
        start = time.time() - 86400
        cur.execute("SELECT COUNT(*) AS c FROM trades WHERE created_at >= ?", (start,))
        return int(cur.fetchone()["c"])

    def touch_source(self, source_id: str, error: str | None = None) -> None:
        cur = self._conn.cursor()
        ts = time.time()
        if error:
            cur.execute(
                """
                INSERT INTO source_health (source_id, last_ok_ts, last_error)
                VALUES (?, 0, ?)
                ON CONFLICT(source_id) DO UPDATE SET last_error=excluded.last_error
                """,
                (source_id, error),
            )
            ok = False
        else:
            cur.execute(
                """
                INSERT INTO source_health (source_id, last_ok_ts, last_error)
                VALUES (?, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET last_ok_ts=excluded.last_ok_ts, last_error=NULL
                """,
                (source_id, ts),
            )
            ok = True
        self._conn.commit()
        from src.dashboard.bus import emit_event

        emit_event(
            "source.health",
            {
                "id": source_id,
                "ok": ok,
                "status": "ok" if ok else "error",
                "last_ts": ts if ok else 0,
                "error": error or "",
            },
        )

    def list_source_health(self) -> list[sqlite3.Row]:
        """全部数据源健康快照。"""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM source_health ORDER BY source_id")
        return cur.fetchall()

    def close(self) -> None:
        self._conn.close()
