"""按数据源独立的异步限流器 + 频次计数。

每个数据源持有自己的 AsyncRateLimiter 实例，互不影响，
从而实现“分开请求”的效果（如 football-data 10/min、api-football 10/min、
thesportsdb 30/min）。限流器采用滑动窗口（默认 60 秒），
并提供非阻塞 try_acquire（拿不到令牌则跳过本次请求，使用上次缓存数据），
避免高频主循环把某个源打爆。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class AsyncRateLimiter:
    """滑动窗口限流器（线程安全，可用于同步/异步场景）。"""

    def __init__(self, max_calls: int, period_sec: float = 60.0, name: str = "") -> None:
        # 窗口内允许的最大调用次数
        self.max_calls = max(1, int(max_calls))
        # 窗口长度（秒）
        self.period_sec = float(period_sec)
        # 源名称，便于日志统计
        self.name = name
        # 记录每次成功获取令牌的时间戳
        self._calls: deque[float] = deque()
        # 累计调用计数（用于统计展示）
        self._total = 0
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        """移除窗口外的旧时间戳。"""
        boundary = now - self.period_sec
        while self._calls and self._calls[0] <= boundary:
            self._calls.popleft()

    def try_acquire(self) -> bool:
        """尝试获取一个令牌；窗口未满返回 True，否则返回 False（不阻塞）。"""
        with self._lock:
            now = time.monotonic()
            self._evict(now)
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                self._total += 1
                return True
            return False

    def used_in_window(self) -> int:
        """当前窗口内已使用的调用次数。"""
        with self._lock:
            self._evict(time.monotonic())
            return len(self._calls)

    def remaining(self) -> int:
        """当前窗口内剩余可用次数。"""
        with self._lock:
            self._evict(time.monotonic())
            return max(0, self.max_calls - len(self._calls))

    @property
    def total(self) -> int:
        """进程启动以来累计成功获取令牌的次数。"""
        return self._total

    def stats(self) -> dict[str, int]:
        """返回限流统计快照，用于结构化日志。"""
        with self._lock:
            self._evict(time.monotonic())
            return {
                "max_per_window": self.max_calls,
                "used": len(self._calls),
                "remaining": max(0, self.max_calls - len(self._calls)),
                "total": self._total,
            }


class MultiWindowRateLimiter:
    """多窗口限流器：同时满足多个 (次数, 窗口秒) 约束才放行。

    典型用途：API-Football 免费层既限 10 次/分钟、又限约 100 次/天，
    两个窗口都还有余量时才允许请求，避免烧光每日配额。
    """

    def __init__(self, windows: list[tuple[int, float]], name: str = "") -> None:
        # 每个窗口对应一个滑动窗口限流器
        self.name = name
        self._limiters = [
            AsyncRateLimiter(max_calls, period_sec, name=f"{name}:{int(period_sec)}s")
            for max_calls, period_sec in windows
        ]
        self._lock = threading.Lock()
        self._total = 0

    def try_acquire(self) -> bool:
        """所有窗口都有余量时放行并各记一次；任一窗口已满则整体拒绝。"""
        with self._lock:
            now = time.monotonic()
            for lim in self._limiters:
                with lim._lock:
                    lim._evict(now)
                    if len(lim._calls) >= lim.max_calls:
                        return False
            # 全部有余量，逐个记一次
            for lim in self._limiters:
                with lim._lock:
                    lim._calls.append(now)
                    lim._total += 1
            self._total += 1
            return True

    @property
    def total(self) -> int:
        return self._total

    def stats(self) -> dict[str, Any]:
        """返回各窗口统计，便于排查每日/每分钟配额消耗。"""
        return {
            "total": self._total,
            "windows": [lim.stats() for lim in self._limiters],
        }
