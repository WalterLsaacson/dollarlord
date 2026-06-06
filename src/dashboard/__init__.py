"""Bot 可视化 Dashboard（FastAPI + WebSocket 事件推送）。"""

from src.dashboard.bus import DashboardBus, get_bus, set_bus

__all__ = ["DashboardBus", "get_bus", "set_bus"]
