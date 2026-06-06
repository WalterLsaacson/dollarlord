# Polymarket 体育赛事结算延迟套利 Bot

在体育赛事**官方终局**后、Polymarket **UMA 正式 resolve** 前，若胜方 outcome token 的卖价仍低于阈值（如 $0.98），则自动分级 FOK 买入，待 resolve 后 redeem。

## 功能概览

- **全自动**：Gamma 发现足球/NBA 盘口 → 免费多源赛果反向匹配 → 终局信号 → 分级下单
- **多源竞速**：`nba_api`、ESPN NBA、ESPN 足球、OpenLigaDB 并行，取最快终局；冲突熔断
- **分级下单**：按 `max_round_notional_usd` 与盘口深度逐级尝试 FOK（50→25→10→5）
- **双环境**：大陆本地代理测试 / 伦敦 VPS 直连生产
- **模式**：`paper`（模拟）/ `live`（真金）
- **7×24**：`systemd` 常驻 + 健康检查 + SQLite 状态恢复

## 快速开始（大陆本地）

### 1. 依赖

```bash
cd ~/projects/polymarket-settlement-arb
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# live 真金需额外：
pip install -e ".[live]"
```

### 2. 代理

确保本地代理监听 `127.0.0.1:1080`（HTTP + SOCKS5），使用 `config.local.yaml`：

```yaml
proxy:
  enabled: true
  socks5_url: "socks5://127.0.0.1:1080"
  http_url: "http://127.0.0.1:1080"
mode: paper
```

### 3. 连通性检查

```bash
python -m src.health config.local.yaml
```

### 4. 启动 bot

```bash
python -m src.main --config config.local.yaml
```

日志：`logs/arb.jsonl`；数据库：`data/arb.db`。

## 伦敦生产部署

1. 将代码同步到 VPS（如 `/opt/polymarket-settlement-arb`）
2. 使用 `config.london.yaml`（`proxy.enabled: false`）
3. 私钥与 CLOB 凭证写入 `/etc/polymarket-arb.env`（`chmod 600`）：

```env
PK=0x...
CLOB_API_KEY=...
CLOB_SECRET=...
CLOB_PASS_PHRASE=...
FUNDER=0x...   # 若使用 proxy wallet
```

4. 安装并启动：

```bash
chmod +x deploy/install.sh deploy/healthcheck.sh
sudo ./deploy/install.sh config.london.yaml
sudo -u polymarket-arb /opt/polymarket-settlement-arb/.venv/bin/python \
  -m src.health /etc/polymarket-arb/config.yaml
# 先 paper 验证后再改 config 中 mode: live
sudo systemctl enable --now polymarket-arb
```

5. 定时健康检查（cron）：

```cron
*/5 * * * * /opt/polymarket-settlement-arb/deploy/healthcheck.sh
```

## 配置说明

| 参数 | 含义 |
|------|------|
| `mode` | `paper` / `live` |
| `entry_max_price` | 最高买入价（如 0.99） |
| `max_round_notional_usd` | 每轮最大尝试金额 |
| `order_ladder_usd` | 分级阶梯（可省略，自动减半） |
| `gamma_sync_interval_sec` | PM 市场同步间隔（默认 900） |
| `market_cooldown_sec` | 单市场成交后冷却 |

## 队名映射

无法自动匹配时日志会出现 `UNMAPPED`。可手动写入 SQLite：

```sql
INSERT INTO team_aliases (alias, canonical, sport) VALUES ('man utd', 'manchester united', 'football');
```

## 风险说明

- UMA **争议**可能导致胜方 token 归零，非无风险套利
- 赛果修正（VAR 等）与 PM 结算口径可能不一致
- 体育市价单有 **1 秒**撮合延迟
- 免费数据源有延迟与稳定性限制

## 项目结构

```
src/main.py           # 7×24 主循环
src/sports/           # 多源赛果
src/pm/               # Gamma + CLOB
src/matcher/          # 反向匹配
src/engine/           # 信号 + 分级下单
deploy/               # systemd / healthcheck
```

## 许可证

MIT
