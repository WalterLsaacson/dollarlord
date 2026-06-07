# Polymarket 体育赛事结算延迟套利 Bot

在体育赛事**官方终局**后、Polymarket **UMA 正式 resolve** 前，若胜方 outcome token 的卖价仍低于阈值（如 $0.99），则自动分级 FOK 买入，待 resolve 后 redeem。

**详细使用说明（启动/停止/配置/成交错过原因）见 [docs/USER_GUIDE.zh.md](docs/USER_GUIDE.zh.md)。**

## 功能概览

- **全自动**：Gamma 发现足球/NBA 盘口 → 免费多源赛果反向匹配 → 终局 / 直播信号 → 分级下单
- **多源竞速**：ESPN、OpenLigaDB、football-data、API-Football、BallDontLie 等并行，取最快终局；冲突熔断
- **开球校验**：同名不同日场次按 `game_start_time` 对齐，避免旧赛果误触发未来盘
- **分级下单**：按 `max_round_notional_usd` 与盘口深度逐级尝试 FOK
- **Dashboard**：内嵌 Web UI（`http://127.0.0.1:8787`），Watchlist / 成交错过 / **持仓结算** / 启停控制
- **持仓结算**：胜方 token 价格 ≥ **0.998** 时自动链上 redeem；Dashboard 可手动结算
- **双环境**：大陆本地代理测试 / 伦敦 VPS 直连生产
- **模式**：`paper`（模拟）/ `live`（真金）

## 快速开始（大陆本地）

### 1. 依赖

```bash
cd polymarket-settlement-arb
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e ".[live]"   # live 真金
cp .env.example polymarket-arb.env   # 编辑 PK / API Key
```

### 2. 启动

```bash
./scripts/start_bot.sh          # 后台运行，默认 config.yaml
# Dashboard: http://127.0.0.1:8787
./scripts/stop_bot.sh           # 停止
```

代理默认 `127.0.0.1:1082`（Shadowrocket），见 `config.yaml` 中 `proxy` 段。

### 3. 健康检查

```bash
python -m src.health config.yaml
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

完整配置项见 **[docs/USER_GUIDE.zh.md](docs/USER_GUIDE.zh.md#3-配置文件说明)**。常用项：

| 参数 | 含义 |
|------|------|
| `mode` | `paper` / `live` |
| `entry_max_price` / `early_entry_price` | 买入价窗口上下限 |
| `max_round_notional_usd` | 每轮最大尝试金额 |
| `early_entry_enabled` | 足球后段直播价早进场 |
| `dashboard_enabled` | 是否启动 Web Dashboard |

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
src/dashboard/        # Web Dashboard
src/engine/           # 信号 + 分级下单 + 开球校验
src/sports/           # 多源赛果
src/pm/               # Gamma + CLOB
src/matcher/          # 反向匹配
docs/USER_GUIDE.zh.md # 使用指南（中文）
deploy/               # systemd / healthcheck
```

## 许可证

MIT

清空历史并重启
./scripts/stop_bot.sh && ./scripts/clear_history.sh && ./scripts/start_bot.sh
手动重启
cd /Users/mando/Documents/polymarket-settlement-arb
./scripts/start_bot.sh