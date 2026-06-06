#!/usr/bin/env bash
# 停止后台 bot（并等待 Dashboard 8787 端口释放，避免重启时 bind 失败）
set -euo pipefail
cd "$(dirname "$0")/.."
PID_FILE="logs/bot.pid"
DASH_PORT="${DASH_PORT:-8787}"

_stop_pid() {
  local pid="$1"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "已发送停止信号，PID=$pid"
  fi
}

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  _stop_pid "$PID"
else
  echo "未找到 $PID_FILE，尝试按命令行结束 src.main"
fi

# 兜底：结束所有 bot 主进程
pkill -f "python.*-m src.main" 2>/dev/null || true

# 等待 Dashboard 端口释放（旧进程 graceful shutdown 可能较慢）
for _ in $(seq 1 30); do
  if ! lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

# 仍占用则强杀
if lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  PIDS=$(lsof -t -iTCP:"$DASH_PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "${PIDS:-}" ]]; then
    kill -9 $PIDS 2>/dev/null || true
    echo "已强制释放端口 $DASH_PORT"
  fi
fi

rm -f "$PID_FILE"
echo "bot 已停止"
