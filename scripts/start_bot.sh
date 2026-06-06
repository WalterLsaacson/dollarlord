#!/usr/bin/env bash
# 启动 live bot（后台运行，默认 config.yaml + 127.0.0.1:1082 代理 Shadowrocket）
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs data

CONFIG="${1:-config.yaml}"
PID_FILE="logs/bot.pid"
LOG_FILE="logs/bot_stdout.log"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "bot 已在运行，PID=$(cat "$PID_FILE")"
  exit 0
fi

if ! nc -z 127.0.0.1 1082 2>/dev/null; then
  echo "[警告] 127.0.0.1:1082 未监听，请先开启 Shadowrocket/Clash；仍将使用 ${CONFIG} (proxy.enabled=true)"
fi

nohup .venv/bin/python -m src.main --config "$CONFIG" >>"$LOG_FILE" 2>&1 < /dev/null &
BPID=$!
disown -h "$BPID" 2>/dev/null || true
echo "$BPID" >"$PID_FILE"
sleep 2
if ! kill -0 "$BPID" 2>/dev/null; then
  echo "[错误] bot 启动后立即退出，请查看 $LOG_FILE"
  exit 1
fi
echo "已启动 live bot，PID=$(cat "$PID_FILE")，配置=$CONFIG"
echo "日志: logs/arb.jsonl 与 $LOG_FILE"
