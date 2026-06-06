"""风控：cooldown、日限额、连续失败暂停。"""

from __future__ import annotations

import logging
import time

from src.config import AppConfig
from src.store.sqlite import Store

logger = logging.getLogger("arb.risk")


class RiskManager:
    """交易风控。"""

    def __init__(self, cfg: AppConfig, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self._consecutive_failures = 0
        self._live_paused = False
        # CLOB 地区限制（云主机伦敦 IP 也可能被 polymarket geoblock）
        self._geoblocked = False

    @property
    def live_paused(self) -> bool:
        return self._live_paused

    @property
    def geoblocked(self) -> bool:
        return self._geoblocked

    def set_geoblocked(self, blocked: bool, reason: str = "") -> None:
        """更新 geoblock 状态；受限时禁止发单并打日志。"""
        if blocked and not self._geoblocked:
            logger.error(
                "CLOB 地区限制：当前出口 IP 无法交易 (%s)。"
                "请在 config 启用 SOCKS5 代理（非云机房 IP）后重启服务。",
                reason or "geoblock",
            )
        elif not blocked and self._geoblocked:
            logger.info("CLOB 地区限制已解除，可恢复下单")
        self._geoblocked = blocked

    def can_trade(self, market_id: str) -> tuple[bool, str]:
        """是否允许对某市场下单。"""
        if self._geoblocked:
            return False, "geoblocked"
        if self._live_paused:
            return False, "live_paused"
        if self.store.is_on_cooldown(market_id):
            return False, "cooldown"
        if self.store.count_trades_today() >= self.cfg.max_daily_trades:
            return False, "daily_limit"
        return True, "ok"

    def record_success(self, market_id: str) -> None:
        self._consecutive_failures = 0
        self.store.set_cooldown(market_id, self.cfg.market_cooldown_sec)

    def record_failure(self, *, count: bool = True) -> None:
        """记录一次下单失败；geoblock / 无效价格等不应计入连续失败。"""
        if not count:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.cfg.max_consecutive_failures:
            self._live_paused = True
            logger.error(
                "连续失败 %d 次，暂停 live 下单",
                self._consecutive_failures,
            )

    def reset_pause(self) -> None:
        self._live_paused = False
        self._consecutive_failures = 0
