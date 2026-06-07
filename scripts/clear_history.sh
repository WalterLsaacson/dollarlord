#!/usr/bin/env bash
# 清空 Dashboard 历史数据与 logs/arb.jsonl（保留 markets 等运行状态）
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${DB_PATH:-data/arb.db}"
mkdir -p logs data

echo "清空 logs/arb.jsonl 与 logs/bot_stdout.log …"
: > logs/arb.jsonl
: > logs/bot_stdout.log

if [[ -f "$DB" ]]; then
  echo "清空 SQLite 成交/错过/结算记录 …"
  sqlite3 "$DB" "
    DELETE FROM trades;
    DELETE FROM signal_events;
    DELETE FROM redemptions;
    DELETE FROM cooldowns;
  "
  echo "已清空 trades / signal_events / redemptions / cooldowns"
else
  echo "数据库不存在，跳过: $DB"
fi

echo "完成。请手动执行 ./scripts/start_bot.sh 重启服务。"
