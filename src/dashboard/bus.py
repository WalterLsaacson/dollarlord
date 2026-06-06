"""Dashboard 事件总线：业务模块状态变更时 emit，Hub 订阅后推 WebSocket。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

# 全局 bus 引用（未启用 dashboard 时为 None，emit 空操作）
_bus: DashboardBus | None = None


def get_bus() -> DashboardBus | None:
    return _bus


def set_bus(bus: DashboardBus | None) -> None:
    global _bus
    _bus = bus


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class DashboardBus:
    """异步事件总线。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._handlers: list[EventHandler] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """在 asyncio 主循环中启动消费者。"""
        self._loop = asyncio.get_running_loop()
        self._consumer_task = asyncio.create_task(self._consume(), name="dashboard_bus")

    async def stop(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """线程安全：可从 sync 代码（Store / logging）调用。"""
        if self._loop is None or self._loop.is_closed():
            return
        data = payload or {}
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._enqueue(event_type, data))
        )

    async def _enqueue(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._queue.put((event_type, payload))

    async def _consume(self) -> None:
        while True:
            event_type, payload = await self._queue.get()
            for handler in list(self._handlers):
                try:
                    await handler(event_type, payload)
                except Exception:
                    pass


def emit_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """便捷 emit：无 bus 时静默。"""
    bus = get_bus()
    if bus is not None:
        bus.emit(event_type, payload)
