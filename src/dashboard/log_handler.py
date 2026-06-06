"""Dashboard 日志 Handler：结构化日志写入时 emit log.append。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.dashboard.bus import emit_event


class DashboardLogHandler(logging.Handler):
    """将 JSON 日志行推送到 Dashboard 事件总线。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
                payload.update(record.extra_data)
            emit_event("log.append", payload)
        except Exception:
            pass
