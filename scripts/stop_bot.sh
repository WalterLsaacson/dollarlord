#!/usr/bin/env bash
# 停止后台 bot
set -euo pipefail
cd "$(dirname "$0")/.."
PID_FILE="logs/bot.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "未找到 $PID_FILE"
  exit 1
fi
PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "已停止 bot，PID=$PID"
else
  # 兼容：PID 文件可能是 shell，尝试按命令行结束
  pkill -f "python.*-m src.main" 2>/dev/null && echo "已按命令行停止 src.main" || echo "进程已不存在，PID=$PID"
fi
rm -f "$PID_FILE"
