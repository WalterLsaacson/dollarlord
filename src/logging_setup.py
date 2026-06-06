"""结构化 JSON 日志。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonLineFormatter(logging.Formatter):
    """每行一条 JSON 日志。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            payload.update(record.extra_data)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_path: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """配置根 logger。"""
    root = logging.getLogger("arb")
    root.setLevel(level)
    root.handlers.clear()

    handler: logging.Handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(JsonLineFormatter())
        root.addHandler(fh)

    return root


def log_event(logger: logging.Logger, msg: str, **kwargs: Any) -> None:
    """带结构化字段的日志。"""
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        "(unknown)",
        0,
        msg,
        (),
        None,
    )
    record.extra_data = kwargs  # type: ignore[attr-defined]
    logger.handle(record)
