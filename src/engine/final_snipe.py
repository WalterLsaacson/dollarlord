"""终局抢单待命（方向三）：比赛尾声预订阅 CLOB，终局一到即可拉簿下单。

与方案 B 直播早进场分离：足球默认关闭直播 ARM，仅在比赛进入尾声分钟门槛后
订阅 watchlist 对应 token，配合高频赛果轮询 + API-Football PRO 终局检测。
"""

from __future__ import annotations

from src.config import AppConfig
from src.engine.football_live_entry import football_effective_minute
from src.sports.base import FixtureStatus, FixtureUpdate, SportType


def is_final_snipe_fixture(f: FixtureUpdate, cfg: AppConfig) -> bool:
    """是否处于「终局抢单待命」窗口（LIVE 且比赛分钟 ≥ final_snipe_minute）。"""
    if not cfg.final_snipe_enabled:
        return False
    if f.sport != SportType.FOOTBALL:
        return False
    if f.status != FixtureStatus.LIVE:
        return False
    minute = football_effective_minute(f)
    if minute is None:
        return False
    return minute >= cfg.final_snipe_minute
