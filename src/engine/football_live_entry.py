"""足球直播早进场资格（方案 B）。

规则（递进，须同时满足「分钟门槛 + 净胜球门槛」才可直播买）：
- 任意时刻仅 1 球领先：不可直播，只走终局 on_final。
- ≥ football_min_elapsed_min 且净胜球 ≥ football_blowout_lead：可 ARM + 直播买。
- 80' 时 1-0 不买；85' 变为 2-0 时分钟与分差均达标，此时才买。
- 80' 前即使 2-0 也不买（须等分钟门槛）。
"""

from __future__ import annotations

from src.config import AppConfig
from src.sports.base import FixtureStatus, FixtureUpdate, SportType


def football_goal_margin(f: FixtureUpdate) -> int | None:
    """净胜球（绝对值）；缺比分则 None。"""
    if f.home_score is None or f.away_score is None:
        return None
    return abs(f.home_score - f.away_score)


def football_effective_minute(f: FixtureUpdate) -> int | None:
    """当前估算比赛分钟：优先数据源 elapsed，否则墙钟。"""
    if f.elapsed_minute is not None:
        return f.elapsed_minute
    return f.match_minute_estimate()


def football_minute_passed_threshold(f: FixtureUpdate, cfg: AppConfig) -> bool:
    """是否已过「可直播」的分钟门槛（真实分钟 vs 墙钟兜底）。"""
    if f.elapsed_minute is not None:
        return f.elapsed_minute >= cfg.football_min_elapsed_min
    wc = f.match_minute_estimate()
    if wc is not None:
        # 无真实分钟时墙钟偏大（含中场），用更高阈值避免过早 ARM
        return wc >= cfg.football_fallback_wallclock_min
    return False


def is_football_live_entry_eligible(f: FixtureUpdate, cfg: AppConfig) -> bool:
    """方案 B：两球及以上领先且已踢满分钟门槛，才允许直播早进场。"""
    if f.sport != SportType.FOOTBALL:
        return False
    if f.status != FixtureStatus.LIVE:
        return False
    margin = football_goal_margin(f)
    if margin is None:
        return False
    # 一球领先：永不直播（等终局）
    if margin < cfg.football_blowout_lead:
        return False
    return football_minute_passed_threshold(f, cfg)


def football_live_entry_block_reason(f: FixtureUpdate, cfg: AppConfig) -> str:
    """不可直播时的原因（日志/调试）。"""
    if f.sport != SportType.FOOTBALL:
        return "not_football"
    if f.status != FixtureStatus.LIVE:
        return f"status_{f.status.value}"
    margin = football_goal_margin(f)
    if margin is None:
        return "no_score"
    if margin < cfg.football_blowout_lead:
        return f"lead_{margin}_wait_final"
    if not football_minute_passed_threshold(f, cfg):
        em = football_effective_minute(f)
        return f"minute_{em}_lt_{cfg.football_min_elapsed_min}"
    return ""
