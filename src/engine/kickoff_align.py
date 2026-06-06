"""开球时间与 PM 市场对齐校验，防止同名不同日误下单。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# 多数据源开球时间可能差数小时；超过此容差视为不同场次
KICKOFF_TOLERANCE_SEC = 18 * 3600
# 市场开球仍在此秒数之后视为「未来盘」，须严格对齐 fixture 开球
FUTURE_MARKET_GRACE_SEC = 2 * 3600


def parse_iso_datetime(raw: str | None) -> datetime | None:
    """解析 ISO8601 时间为 UTC。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def parse_market_kickoff(row: Any) -> datetime | None:
    """从 markets 行读取 game_start_time。"""
    if row is None:
        return None
    gst = row["game_start_time"] if hasattr(row, "__getitem__") else None
    return parse_iso_datetime(gst)


def kickoff_delta_sec(a: datetime | None, b: datetime | None) -> float | None:
    """两开球时间的绝对差（秒）。"""
    if a is None or b is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return abs((a - b).total_seconds())


def kickoffs_aligned(
    market_ko: datetime | None,
    fixture_ko: datetime | None,
    tolerance_sec: float = KICKOFF_TOLERANCE_SEC,
) -> bool:
    """PM 市场开球与赛果源开球是否在容差内。"""
    delta = kickoff_delta_sec(market_ko, fixture_ko)
    if delta is None:
        return False
    return delta <= tolerance_sec


def market_is_future(market_ko: datetime | None, now: datetime | None = None) -> bool:
    """市场计划开球是否仍在未来（含缓冲）。"""
    if market_ko is None:
        return False
    now = now or datetime.now(timezone.utc)
    if market_ko.tzinfo is None:
        market_ko = market_ko.replace(tzinfo=timezone.utc)
    return market_ko > now + timedelta(seconds=FUTURE_MARKET_GRACE_SEC)


def final_allowed_for_market(
    row: Any,
    *,
    fixture_kickoff: datetime | None,
    observed_at: datetime | None,
    tolerance_sec: float = KICKOFF_TOLERANCE_SEC,
) -> tuple[bool, str]:
    """终局赛果是否允许关联到该 PM 市场。"""
    market_ko = parse_market_kickoff(row)
    now = datetime.now(timezone.utc)

    if market_is_future(market_ko, now):
        # 未来盘：必须与 fixture 开球对齐，且终局观测不能早于计划开球
        if not kickoffs_aligned(market_ko, fixture_kickoff, tolerance_sec):
            return False, "future_market_kickoff_mismatch"
        if observed_at and market_ko:
            obs = observed_at
            if obs.tzinfo is None:
                obs = obs.replace(tzinfo=timezone.utc)
            if obs < market_ko - timedelta(minutes=30):
                return False, "final_before_scheduled_kickoff"
        return True, ""

    if market_ko and fixture_kickoff:
        if not kickoffs_aligned(market_ko, fixture_kickoff, tolerance_sec):
            return False, "kickoff_mismatch"

    if market_ko and observed_at:
        obs = observed_at
        if obs.tzinfo is None:
            obs = obs.replace(tzinfo=timezone.utc)
        if obs < market_ko - timedelta(hours=1):
            return False, "final_before_scheduled_kickoff"

    return True, ""


def pick_market_by_kickoff(
    rows: list[Any],
    fixture_kickoff: datetime | None,
    tolerance_sec: float = KICKOFF_TOLERANCE_SEC,
) -> Any | None:
    """多名同对阵候选时，选开球时间最接近 fixture 的一个。"""
    if not rows:
        return None
    if len(rows) == 1:
        row = rows[0]
        if fixture_kickoff is None:
            return row
        mk = parse_market_kickoff(row)
        if mk is None:
            return None
        if kickoffs_aligned(mk, fixture_kickoff, tolerance_sec):
            return row
        return None

    if fixture_kickoff is None:
        return None

    best: Any | None = None
    best_delta: float | None = None
    for row in rows:
        mk = parse_market_kickoff(row)
        delta = kickoff_delta_sec(mk, fixture_kickoff)
        if delta is None:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = row
    if best is not None and best_delta is not None and best_delta <= tolerance_sec:
        return best
    return None
