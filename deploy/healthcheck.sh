#!/usr/bin/env bash
# 端点连通 + 进程存活检查（与 config 共用代理环境）

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/polymarket-settlement-arb}"
CONFIG="${CONFIG:-$INSTALL_DIR/config.london.yaml}"
VENV_PYTHON="$INSTALL_DIR/.venv/bin/python"

cd "$INSTALL_DIR"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "FAIL: venv not found"
  exit 1
fi

# API 连通
if ! "$VENV_PYTHON" -m src.health "$CONFIG"; then
  echo "FAIL: health endpoints"
  exit 1
fi

# systemd 进程（生产环境）
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet polymarket-arb 2>/dev/null; then
    echo "OK: polymarket-arb service active"
  else
    echo "WARN: polymarket-arb service not active (skip if local test)"
  fi
fi

echo "OK: healthcheck passed"
exit 0
