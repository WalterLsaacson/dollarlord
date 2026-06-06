#!/usr/bin/env bash
# Ubuntu 安装脚本（伦敦生产示例）

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/polymarket-settlement-arb}"
CONFIG_SRC="${1:-config.london.yaml}"

echo "==> 安装到 $INSTALL_DIR"

sudo useradd -r -s /bin/false polymarket-arb 2>/dev/null || true

sudo mkdir -p "$INSTALL_DIR" /etc/polymarket-arb
sudo rsync -a --exclude '.venv' --exclude 'data' --exclude 'logs' ./ "$INSTALL_DIR/"

cd "$INSTALL_DIR"
sudo python3 -m venv .venv
sudo .venv/bin/pip install -U pip
sudo .venv/bin/pip install -e ".[live]"

sudo mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
sudo chown -R polymarket-arb:polymarket-arb "$INSTALL_DIR"

if [[ -f "$CONFIG_SRC" ]]; then
  sudo cp "$CONFIG_SRC" /etc/polymarket-arb/config.yaml
fi

if [[ ! -f /etc/polymarket-arb.env ]]; then
  echo "# 填入 PK 与 CLOB 凭证" | sudo tee /etc/polymarket-arb.env
  sudo chmod 600 /etc/polymarket-arb.env
fi

sudo cp deploy/polymarket-arb.service /etc/systemd/system/
sudo systemctl daemon-reload

echo "==> 完成。下一步："
echo "  1. 编辑 /etc/polymarket-arb.env"
echo "  2. sudo -u polymarket-arb $INSTALL_DIR/.venv/bin/python -m src.health /etc/polymarket-arb/config.yaml"
echo "  3. sudo systemctl enable --now polymarket-arb"
