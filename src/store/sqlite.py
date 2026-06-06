"""SQLite 持久化：市场、映射、成交、cooldown。"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
            """
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

    def get_market(self, market_id: str) -> sqlite3.Row | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM markets WHERE market_id=?", (market_id,))
        return cur.fetchone()

    def get_market_by_token(self, token_id: str) -> sqlite3.Row | None:
        """按 yes/no token_id 反查市场（用于盘口回调定位市场）。"""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM markets WHERE token_yes=? OR token_no=? LIMIT 1",
            (token_id, token_id),
        )
        return cur.fetchone()

    def set_watch_state(self, market_id: str, state: str, winner_side: str | None = None) -> None:
        cur = self._conn.cursor()
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
    ) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO trades (market_id, mode, notional_usd, price, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (market_id, mode, notional_usd, price, status, detail, time.time()),
        )
        self._conn.commit()

    def count_trades_today(self) -> int:
        cur = self._conn.cursor()
        start = time.time() - 86400
        cur.execute("SELECT COUNT(*) AS c FROM trades WHERE created_at >= ?", (start,))
        return int(cur.fetchone()["c"])

    def touch_source(self, source_id: str, error: str | None = None) -> None:
        cur = self._conn.cursor()
        if error:
            cur.execute(
                """
                INSERT INTO source_health (source_id, last_ok_ts, last_error)
                VALUES (?, 0, ?)
                ON CONFLICT(source_id) DO UPDATE SET last_error=excluded.last_error
                """,
                (source_id, error),
            )
        else:
            cur.execute(
                """
                INSERT INTO source_health (source_id, last_ok_ts, last_error)
                VALUES (?, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET last_ok_ts=excluded.last_ok_ts, last_error=NULL
                """,
                (source_id, time.time()),
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
