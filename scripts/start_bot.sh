#!/usr/bin/env bash
# 启动 live bot（后台运行，默认 config.yaml + 127.0.0.1:1082 代理 Shadowrocket）
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs data

CONFIG="${1:-config.yaml}"
PID_FILE="logs/bot.pid"
LOG_FILE="logs/bot_stdout.log"
DASH_PORT="${DASH_PORT:-8787}"

# 清理失效 PID 文件
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if ! kill -0 "$OLD_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
  fi
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "bot 已在运行，PID=$(cat "$PID_FILE")"
  exit 0
fi

# 8787 仍被占用则先 stop（常见于上次重启竞态）
if lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[警告] 端口 $DASH_PORT 被占用，先执行 stop_bot.sh"
  ./scripts/stop_bot.sh || true
  sleep 1
fi

if ! nc -z 127.0.0.1 1082 2>/dev/null; then
  echo "[警告] 127.0.0.1:1082 未监听，请先开启 Shadowrocket/Clash；仍将使用 ${CONFIG} (proxy.enabled=true)"
fi

nohup .venv/bin/python -m src.main --config "$CONFIG" >>"$LOG_FILE" 2>&1 < /dev/null &
BPID=$!
disown -h "$BPID" 2>/dev/null || true
echo "$BPID" >"$PID_FILE"

# 等待进程稳定 + Dashboard 监听
for _ in $(seq 1 20); do
  sleep 0.5
  if ! kill -0 "$BPID" 2>/dev/null; then
    echo "[错误] bot 启动后立即退出，请查看 $LOG_FILE"
    tail -20 "$LOG_FILE" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
done

if ! kill -0 "$BPID" 2>/dev/null; then
  echo "[错误] bot 未运行，请查看 $LOG_FILE"
  exit 1
fi

if ! lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[警告] bot 进程在运行但 Dashboard 未监听 ${DASH_PORT}，请查看 $LOG_FILE"
else
  echo "Dashboard 已监听 http://127.0.0.1:$DASH_PORT"
fi

echo "已启动 live bot，PID=$(cat "$PID_FILE")，配置=$CONFIG"
echo "日志: logs/arb.jsonl 与 $LOG_FILE"
