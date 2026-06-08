"""Gamma API：发现足球/NBA 市场。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import AppConfig
from src.logging_setup import log_event
from src.net.proxy import ProxyTransport
from src.store.sqlite import MarketRow, Store

logger = logging.getLogger("arb.gamma")

# Polymarket sports tag 常见值（Gamma /sports 可刷新）
NBA_TAG_HINTS = ("nba", "basketball")
FOOTBALL_TAG_HINTS = ("soccer", "football", "epl", "ucl", "mls", "la liga", "bundesliga")
NBA_EXCLUDE_HINTS = ("wnba",)
FOOTBALL_EXCLUDE_HINTS = ("nfl", "ncaa football", "cfp", "super bowl")
# 过滤非"单场对阵"市场（冠军、奖项、选举等）
NON_MATCH_KEYWORDS = (
    "governor",
    "president",
    "election",
    "world cup",
    "champion",
    "winner",
    "mvp",
    "ballon d'or",
    "player v",
    "team v",
    "placeholder v",
    "fighter v",
    "draw?",
    "o/u",
    "over/under",
    "both teams to score",
    "handicap",
    "total sets",
    "set handicap",
    "map handicap",
    "series",
)


class GammaSync:
    """从 Gamma 同步体育赛事市场。"""

    def __init__(self, cfg: AppConfig, proxy: ProxyTransport, store: Store) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.store = store

    async def sync_markets(self) -> list[MarketRow]:
        """拉取并持久化足球/NBA/MLB/NHL/NFL 相关市场。"""
        client = await self.proxy.get_httpx_client()
        markets: list[MarketRow] = []

        # 通过 events 接口筛选体育
        for sport_filter, sport_name in [
            ("nba", "nba"),
            ("football", "football"),
            ("mlb", "mlb"),
            ("nhl", "nhl"),
            ("nfl", "nfl"),
        ]:
            if sport_name not in self.cfg.sports and sport_filter not in self.cfg.sports:
                continue
            try:
                rows = await self._fetch_sport_markets(client, sport_filter, sport_name)
                markets.extend(rows)
            except Exception as e:
                logger.error("Gamma 同步 %s 失败: %s", sport_filter, e)

        return markets

    async def _fetch_sport_markets(
        self,
        client: Any,
        tag_hint: str,
        sport: str,
    ) -> list[MarketRow]:
        rows: list[MarketRow] = []
        seen_market_ids: set[str] = set()
        offset = 0
        limit = 100
        scanned_events = 0
        scanned_markets = 0
        while True:
            url = f"{self.cfg.gamma_base_url}/events"
            params = {
                "closed": "false",
                "tag_slug": "sports",
                "limit": limit,
                "offset": offset,
            }
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            events = resp.json()
            if not events:
                break
            scanned_events += len(events)
            for ev in events:
                markets = ev.get("markets") or []
                event_title = str(ev.get("title", ""))
                event_text = f"{event_title} {ev.get('description', '')}".lower()
                event_tags = {
                    str(t.get("slug", "")).lower()
                    for t in (ev.get("tags") or [])
                    if isinstance(t, dict)
                }
                scanned_markets += len(markets)
                for m in markets:
                    row = self._parse_market(m, sport, tag_hint, event_title, event_text, event_tags)
                    if row and row.market_id not in seen_market_ids:
                        self.store.upsert_market(row)
                        rows.append(row)
                        seen_market_ids.add(row.market_id)
            if len(events) < limit:
                break
            offset += limit
            # 体育类 event 总量约 3000，cap 设高一些，避免漏掉排序靠后的
            # NBA 决赛 / 足球单场（实测 2000 cap 会截断丢失决赛盘）
            if offset >= 8000:
                break
        log_event(
            logger,
            "GAMMA_DISCOVERY",
            sport=sport,
            scanned_events=scanned_events,
            scanned_markets=scanned_markets,
            candidate_markets=len(rows),
        )
        return rows

    def _parse_market(
        self,
        m: dict,
        sport: str,
        tag_hint: str,
        event_title: str,
        event_text: str,
        event_tags: set[str],
    ) -> MarketRow | None:
        question = (m.get("question") or "").lower()
        slug = (m.get("slug") or "").lower()
        tags = json.dumps(m.get("tags") or []).lower()
        text = f"{event_text} {question} {slug} {tags}"

        # 先挡掉明显非单场盘口，避免污染 watchlist
        if any(k in text for k in NON_MATCH_KEYWORDS):
            return None

        market_type = (m.get("sportsMarketType") or "").lower()
        # 仅保留主胜负盘，避免 O/U、让分等不适配当前策略的盘口
        if market_type and market_type not in {"moneyline"}:
            return None

        is_nba = "nba" in event_tags
        is_fb = "soccer" in event_tags
        is_mlb = "mlb" in event_tags or "baseball" in event_tags
        is_nhl = "nhl" in event_tags or "hockey" in event_tags
        is_nfl = "nfl" in event_tags or "american-football" in event_tags
        if sport == "nba" and any(h in text for h in NBA_EXCLUDE_HINTS):
            return None
        if sport == "football" and any(h in text for h in FOOTBALL_EXCLUDE_HINTS):
            return None
        if sport == "nba" and not is_nba:
            return None
        if sport == "nba" and "games" not in event_tags:
            return None
        if sport == "football" and not is_fb:
            return None
        # 单场对阵盘普遍带 "games" tag（实测足球单场 100% 命中），而冠军/奖项/
        # 期货盘不带。用它做正向过滤即可放行世界杯/国际友谊赛/J联赛/西乙等所有单场，
        # 同时挡掉期货盘——比写死联赛白名单覆盖面大得多，避免漏掉 PM 实际开的盘。
        if sport == "football" and "games" not in event_tags:
            return None
        if sport == "mlb" and not is_mlb:
            return None
        if sport == "mlb" and "games" not in event_tags:
            return None
        if sport == "nhl" and not is_nhl:
            return None
        if sport == "nhl" and "games" not in event_tags:
            return None
        if sport == "nfl" and not is_nfl:
            return None
        if sport == "nfl" and "games" not in event_tags:
            return None

        # 解析 token ids
        clob_ids = m.get("clobTokenIds")
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except json.JSONDecodeError:
                clob_ids = []
        if not clob_ids or len(clob_ids) < 2:
            return None

        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = ["Yes", "No"]

        # teamAID/teamBID 常是数字 ID，不是队名；优先取 teamA/teamB 文本
        team_a = m.get("teamA")
        team_b = m.get("teamB")
        # 从 question 提取队名
        if not team_a or not team_b:
            team_a, team_b = self._teams_from_question(m.get("question", ""))
        # 一些盘口问题本身没有对阵信息，回退用 event title 提取
        if not team_a or not team_b:
            team_a, team_b = self._teams_from_question(event_title)
        # 仅保留可识别双方队名的"单场对阵"市场
        if not team_a or not team_b:
            return None
        # 仅保留近期比赛，避免把很久以前的盘子加入候选
        if not self._is_recent_market(m):
            return None

        closed = bool(m.get("closed", False))
        if closed:
            return None

        market_id = str(m.get("id", m.get("conditionId", "")))
        if not market_id:
            return None

        return MarketRow(
            market_id=market_id,
            condition_id=str(m.get("conditionId", "")),
            question=m.get("question", ""),
            sport=sport,
            token_id_yes=str(clob_ids[0]),
            token_id_no=str(clob_ids[1]),
            outcome_names=outcomes if isinstance(outcomes, list) else ["Yes", "No"],
            game_start_time=m.get("gameStartTime"),
            team_a=str(team_a) if team_a else None,
            team_b=str(team_b) if team_b else None,
            closed=closed,
            uma_status=m.get("umaResolutionStatus"),
            slug=m.get("slug"),
            watch_state="unmapped",
        )

    def _is_recent_market(self, m: dict) -> bool:
        """只保留近期赛程（过去 7 天到未来 10 天）。

        未来窗口放宽到 10 天：确保 NBA 决赛、未来几日的赛事能提前进入 watchlist，
        不会因为开赛日离今天稍远而被漏掉（无开赛时间字段的盘默认保留）。
        """
        ts_raw = (
            m.get("gameStartTime")
            or m.get("startDate")
            or m.get("startDateIso")
            or m.get("endDate")
            or m.get("endDateIso")
        )
        if not ts_raw:
            return True
        try:
            dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            return True
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - timedelta(days=7)) <= dt <= (now + timedelta(days=10))

    def _teams_from_question(self, question: str) -> tuple[str | None, str | None]:
        """从 question 文本启发式提取两队。"""
        for sep in (" vs. ", " vs ", " v "):
            if sep in question:
                parts = question.split(sep, 1)
                if len(parts) == 2:
                    left = re.sub(r"^will\s+", "", parts[0].strip(), flags=re.I)
                    right = parts[1].split("?")[0].strip()
                    right = re.sub(r"\s+end in a draw$", "", right, flags=re.I)
                    return left, right
        return None, None

    async def refresh_market_status(self, market_id: str) -> dict | None:
        """刷新单个市场状态。"""
        client = await self.proxy.get_httpx_client()
        url = f"{self.cfg.gamma_base_url}/markets/{market_id}"
        resp = await client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
