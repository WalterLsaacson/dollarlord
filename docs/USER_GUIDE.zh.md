# Polymarket 结算套利 Bot — 使用指南

本文档说明如何在本机 **启动 / 停止 / 重启** bot、**配置项含义**，以及 Dashboard **成交 / 错过** 记录中各字段与原因码的含义。

---

## 1. 前置条件

```bash
cd /path/to/polymarket-settlement-arb
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e ".[live]"   # 真金下单需要
```

| 文件 | 用途 |
|------|------|
| `config.yaml` | 主配置（模式、代理、策略、轮询间隔等） |
| `polymarket-arb.env` | 私钥与 API Key（启动时自动加载，**勿提交 git**） |

首次运行前创建目录（脚本也会自动创建）：

```bash
mkdir -p logs data
```

**代理**：大陆本机通常需 Shadowrocket / Clash 监听 `127.0.0.1:1082`（与 `config.yaml` 中 `proxy` 端口一致）。访问 Dashboard 时浏览器需对 `127.0.0.1` 绕过系统代理。

**健康检查**（可选，启动前建议跑一次）：

```bash
python -m src.health config.yaml
```

`geoblock` 必须为通过，否则 CLOB 无法下单。

---

## 2. 启动 / 停止 / 重启

### 2.1 推荐：后台脚本（本机长期运行）

```bash
# 启动（默认 config.yaml）
./scripts/start_bot.sh

# 指定配置文件
./scripts/start_bot.sh config.local.yaml

# 停止
./scripts/stop_bot.sh
```

| 脚本 | 行为 |
|------|------|
| `start_bot.sh` | `nohup` 后台运行，PID 写入 `logs/bot.pid`，标准输出 → `logs/bot_stdout.log` |
| `stop_bot.sh` | 停止 bot 并**等待 8787 端口释放**（避免重启时 Dashboard bind 失败） |

启动成功后会输出：

- 结构化日志：`logs/arb.jsonl`
- Dashboard：`http://127.0.0.1:8787`（需 `dashboard_enabled: true`）

**建议**在 macOS **Terminal.app** 或 iTerm 中执行脚本；在 IDE 内置终端里后台进程可能被回收。

### 2.2 Dashboard 控制（bot 已在运行时）

打开 `http://127.0.0.1:8787`，顶部有 **启动 / 停止 / 重启** 按钮，对应 REST：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/bot/status` | GET | 查询是否在运行及 PID |
| `/api/start` | POST | 调用 `start_bot.sh` |
| `/api/stop` | POST | 调用 `stop_bot.sh` |
| `/api/restart` | POST | 先 stop，约 2 秒后再 start |

**注意**：

- 点击 **停止** 会结束整个 bot 进程，**Dashboard 也会一起退出**。
- 停止后需重新执行 `./scripts/start_bot.sh`；**重启前若 Dashboard 打不开**，先 `./scripts/stop_bot.sh` 等 8787 释放再启动。
- **重启** 适用于改完配置或代码后热更新进程。

### 2.3 前台调试（开发用）

```bash
source .venv/bin/activate
python -m src.main --config config.yaml
```

前台运行时日志直接打在终端；Ctrl+C 退出。此模式下 Dashboard 按钮会额外拉起/停止后台脚本，与当前前台进程可能并存，**日常运行请用 2.1 脚本**。

---

## 3. 配置文件说明

主配置为 YAML（如 `config.yaml`）。敏感 Key 优先写在 `polymarket-arb.env`，YAML 留空即可。

### 3.1 运行模式

| 配置项 | 类型 | 默认 | 含义 |
|--------|------|------|------|
| `environment` | `local_cn` \| `london` | `local_cn` | 部署环境标识（影响日志语义，不改变核心逻辑） |
| `mode` | `paper` \| `live` | `paper` | `paper` 模拟成交；`live` 真金 FOK 下单 |
| `proxy.enabled` | bool | `false` | 是否通过代理访问 Polymarket / 部分数据源 |
| `proxy.socks5_url` | string | — | SOCKS5 地址，如 `socks5://127.0.0.1:1082` |
| `proxy.http_url` | string | — | HTTP 代理地址 |

环境变量 `SOCKS5_PROXY` / `HTTP_PROXY` 会覆盖 YAML 中的 proxy 设置。

### 3.2 交易与策略

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `entry_max_price` | `0.99` | 买入价上限；卖价高于此值不下单 |
| `early_entry_price` | `0.60` | 买入价下限；卖价低于此值不下单（窗口 `[early_entry_price, entry_max_price]`） |
| `max_round_notional_usd` | `50` | 单轮最大尝试金额（美元） |
| `order_ladder_usd` | 省略 | 分级阶梯，如 `[10, 5]`；省略则按 `max_round_notional_usd` 每次减半 |
| `min_ladder_step_usd` | `5` | 阶梯最小一档金额 |
| `market_cooldown_sec` | `3600` | 某市场成交后冷却秒数，冷却内不再下单 |
| `early_entry_enabled` | `true` | 是否启用「价格驱动早进场」（足球后段直播价买入） |
| `football_min_elapsed_min` | `80` | 足球：真实比赛分钟 ≥ 此值才武装直播监控 |
| `football_fallback_wallclock_min` | `95` | 足球：无真实分钟时，用开赛后墙钟分钟兜底 |
| `nba_early_entry_enabled` | `false` | NBA 是否允许第四节起直播早进场；默认 `false` 仅终局下单 |
| `nba_min_period` | `4` | NBA 早进场：至少第几节 |

**两条下单路径**：

1. **终局路径**（`on_final`）：官方赛果 FINAL → 校验开球时间 → 价格在窗口内 → 分级 FOK。
2. **直播路径**（`on_book_update`）：足球进入后段且已 ARMED → 盘口卖价进入窗口 → 买领先方 token。

### 3.3 轮询与同步

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `gamma_sync_interval_sec` | `900` | 从 Gamma 同步 PM 市场列表间隔（秒） |
| `sports_poll_live_sec` | `2` | 有进行中/武装比赛时，赛果源轮询间隔 |
| `sports_poll_idle_sec` | `60` | 无活跃比赛时赛果源轮询间隔 |
| `clob_eligible_poll_sec` | `1.5` | 已 ARMED 市场的盘口轮询间隔 |
| `conflict_window_sec` | `120` | 多源赛果冲突判定时间窗 |
| `health_interval_sec` | `300` | 健康检查间隔 |

### 3.4 数据源限流

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `rate_football_data_per_min` | `10` | football-data.org 每分钟请求上限 |
| `rate_api_football_per_min` | `10` | API-Football 每分钟上限 |
| `api_football_daily_quota` | `95` | API-Football 每日配额保护 |
| `rate_thesportsdb_per_min` | `30` | TheSportsDB |
| `rate_balldontlie_per_min` | `5` | BallDontLie（NBA） |
| `rate_espn_per_min` | `60` | ESPN 足球 / NBA |
| `rate_openligadb_per_min` | `15` | OpenLigaDB |

### 3.5 体育范围

| 配置项 | 含义 |
|--------|------|
| `sports` | 启用的运动，如 `[football, nba]` |
| `espn_soccer_leagues` | ESPN 足球联赛 slug 列表 |
| `openligadb_leagues` | OpenLigaDB 联赛 shortcut |
| `football_data_competitions` | football-data.org 竞赛代码 |
| `thesportsdb_sports` | TheSportsDB 运动类型 |

### 3.6 风控

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `max_daily_trades` | `100` | 当日成交笔数上限 |
| `max_consecutive_failures` | `5` | 连续下单失败后暂停 live |

### 3.7 持仓结算（redeem）

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `auto_redeem_enabled` | `true` | 是否自动轮询并结算胜方持仓 |
| `redeem_poll_sec` | `120` | 自动结算轮询间隔（秒） |
| `redeem_price_threshold` | `0.998` | Data API 的 `curPrice` ≥ 此值视为胜方「100%」，触发**自动**结算（0.998 = 99.8%） |
| `polygon_rpc_url` | `https://polygon-rpc.com` | Polygon RPC（链上 redeem 用） |

**前提**（live 模式）：

- `polymarket-arb.env` 中已配置 `FUNDER`（Proxy/Safe 地址）与 `PK`（Safe 所有者 EOA 私钥）
- EOA 钱包内有少量 **POL** 支付 gas（redeem 走 Safe `execTransaction`）
- 已安装：`pip install -e ".[live]"`（含 `web3` / `eth-abi` / `eth-keys`）

**逻辑**：

- **自动**：每 `redeem_poll_sec` 拉取持仓；`redeemable=true` 且 `curPrice ≥ 0.998`（99.8%）的胜方场次，链上调用 `redeemPositions` 换回 USDC。
- **手动**：Dashboard「持仓结算」面板可单场结算，或「结算全部胜方 / 全部可赎回」（手动不受 0.998 限制，只要 `redeemable` 即可）。

### 3.8 Dashboard 与存储

| 配置项 | 默认 | 含义 |
|--------|------|------|
| `dashboard_enabled` | `true` | 是否启动内嵌 Web Dashboard |
| `dashboard_host` | `127.0.0.1` | 监听地址 |
| `dashboard_port` | `8787` | 监听端口 |
| `db_path` | `data/arb.db` | SQLite 状态库 |
| `log_path` | `logs/arb.jsonl` | JSONL 结构化日志 |
| `webhook_url` | 空 | 可选告警 Webhook |

### 3.8 环境变量（`polymarket-arb.env`）

| 变量 | 必填 | 含义 |
|------|------|------|
| `PK` | live | 钱包私钥 |
| `FUNDER` | 视钱包类型 | Proxy / 充值钱包地址 |
| `CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASS_PHRASE` | 可选 | CLOB API 三件套；留空则启动时 derive |
| `FOOTBALL_DATA_API_KEY` | 可选 | football-data.org |
| `API_FOOTBALL_KEY` | 可选 | API-Football |
| `BALLDONTLIE_KEY` | 可选 | BallDontLie NBA |
| `THESPORTSDB_KEY` | 可选 | 默认免费 key `123` |
| `SOCKS5_PROXY` / `HTTP_PROXY` | 可选 | 覆盖 YAML 代理 |

未配置的 Key 对应数据源会自动禁用，Dashboard 健康卡片会显示「未配置 API Key，已跳过」。

---

## 4. Dashboard 界面说明

### 4.1 Watchlist（监听列表）

仅展示 **未来或进行中** 的比赛（已结束超过 4 小时的不显示）。每页 10 条，时间均为 **北京时间**。

| 列 | 含义 |
|----|------|
| 开赛 | PM 市场 `game_start_time`（北京时区） |
| 对阵 | `team_a vs team_b` |
| 比分 | 当前赛果源实时比分 |
| 进度 | 足球：`80'` 等比赛分钟；NBA：`P4` 节数 |
| Yes / No | 两侧 token 最优卖价（ask） |
| ARMED | 已进入直播买入监控（足球后段等） |

### 4.2 焦点区（Focus）

展示当前最值得关注的一场：比分、开赛时间、Yes/No 报价、是否 ARMED。

### 4.4 持仓结算

展示 `FUNDER` 钱包在 Polymarket 的持仓：

| 列 | 含义 |
|----|------|
| 场次 | 市场标题 |
| 份额 | 持有 outcome token 数量 |
| 价格% | 当前价 × 100；**≥ 99.8%** 时自动结算（对应 `redeem_price_threshold: 0.998`） |
| 状态 | 胜方 / 可赎回 / 已结算 |
| 操作 | 单场「结算」按钮 |

顶部按钮：**结算全部胜方**（仅价格 ≥ 99.8%）、**结算全部可赎回**（含输家清零，不要求 0.998）。

结算成功会写入历史记录，类型为 **结算**（`kind: redeem`），日志事件 `REDEEM_OK` / `REDEEM_FAIL`。

### 4.5 历史（成交 / 错过 / 结算）

合并 `trades` 与 `signal_events` 表，按时间倒序。类型标签：

- **成交**（`kind: success`）：真实或 paper 成交
- **错过**（`kind: missed`）：策略跳过或未成交
- **结算**（`kind: redeem`）：链上 redeem 成功/失败记录

---

## 5. 成交与错过：字段含义

### 5.1 历史记录通用字段

| 字段 | 成交 | 错过 | 含义 |
|------|------|------|------|
| `created_at` / 时间列 | ✓ | ✓ | Unix 时间戳；界面显示为北京时间 |
| `kind` | `success` | `missed` | 成交 vs 错过 |
| `market_id` | ✓ | ✓ | Polymarket 市场 ID |
| `question` | ✓ | ✓ | 市场标题 |
| `sport` | ✓ | ✓ | `football` / `nba` |
| `team_a` / `team_b` | ✓ | ✓ | 解析出的对阵双方 |
| `price` | 成交价 | 触发时 ask 或 0 | 成交记录为成交价；错过记录为当时盘口价（若有） |
| `notional_usd` | ✓ | — | 成交金额（美元），仅成交 |
| `mode` | ✓ | — | `paper` / `live`，仅成交 |
| `event_type` | `trade` | 见下表 | 事件大类 |
| `reason` | 成交状态 | 跳过原因码 | 见下表 |
| `detail` | CLOB 回报摘要 | 人类可读补充说明 | 鼠标悬停可看完整 detail |

### 5.2 成交（`event_type: trade`）常见 `reason`

| reason | 含义 |
|--------|------|
| `matched` | CLOB 即时撮合成功 |
| `delayed` | 订单已提交，异步撮合（仍计为成功） |
| `paper_matched` | paper 模式模拟成交 |

### 5.3 错过 — `event_type` 分类

| event_type | 含义 |
|------------|------|
| `skip` | 策略评估阶段主动跳过（价格、UMA、开球等） |
| `order_not_filled` | 已发信号但分级下单未成交 |
| `risk_block` | 风控拦截 |
| `done_no_trade` | 比赛已标记结束，全程未成交 |

### 5.4 错过 — `reason` 原因码详解

#### 策略评估（终局 / 直播，未进入下单）

| reason | 含义 | 常见处理 |
|--------|------|----------|
| `result_conflict` | 多源赛果在冲突窗口内不一致 | 等待源一致或人工确认 |
| `future_market_kickoff_mismatch` | 未来盘口与赛果开球时间不对齐（防同名不同日误单） | 正常保护，无需处理 |
| `kickoff_mismatch` | 市场开球与 fixture 开球相差超过约 18 小时 | 检查是否匹配错场次 |
| `final_before_scheduled_kickoff` | 终局观测时间早于计划开球 | 旧赛果污染，已拦截 |
| `market_not_in_db` | 本地库无此市场 | 检查 Gamma 同步 |
| `uma_resolved_or_disputed` | UMA 已 resolve 或 disputed | 市场已无套利空间 |
| `market_closed` | PM 市场已关闭 | — |
| `draw_or_unknown_winner_token` | 平局或无法映射胜方 token | MVP 不做平局盘 |
| `orderbook_fetch_failed` | 拉取订单簿失败 | 查代理 / CLOB 连通性 |
| `no_ask` | 订单簿无卖单 | 流动性不足 |
| `ask_too_low` | 卖价 &lt; 0.01，疑似输家 token | — |
| `ask_above_max` | 卖价 &gt; `entry_max_price`，无套利空间 | 正常错过 |
| `ask_below_min` | 卖价 &lt; `early_entry_price` | 正常错过 |

#### 风控（`event_type: risk_block`）

| reason | 含义 |
|--------|------|
| `geoblocked` | 出口 IP 被 Polymarket 地区限制 |
| `live_paused` | 连续失败过多，live 已暂停 |
| `cooldown` | 该市场仍在成交冷却期 |
| `daily_limit` | 当日成交笔数达上限 |

#### 下单执行（`event_type: order_not_filled`）

| reason / status | 含义 |
|-----------------|------|
| `ILLIQUID` | 深度不足或 FOK 未填满 |
| `no_edge` | 价格不在配置窗口内 |
| `insufficient_balance` | USDC 余额或授权不足 |
| `FOK_NOT_FILLED` | FOK 订单未成交 |
| `geoblocked` | 提交时被地区限制 |
| `auth_error` | CLOB API 凭证无效 |
| `invalid_price` | 价格不符合 CLOB 规则 |
| `ladder_exhausted` | 所有阶梯均未成交 |
| `skipped` | 被风控等前置条件挡下 |

#### 其他

| reason | 含义 |
|--------|------|
| `done_no_trade` | 该场 watch_state 变为 `done` 但从未成交 |

---

## 6. 结构化日志（`logs/arb.jsonl`）

Dashboard 历史来自 SQLite；完整链路可查 JSONL。与策略相关的主要 `event`：

| event | 含义 |
|-------|------|
| `STRATEGY_EVAL` | 收到终局，开始评估 |
| `STRATEGY_SIGNAL` | 价格满足，开始分级下单 |
| `STRATEGY_SKIP` | 跳过及 `reason` |
| `STRATEGY_NO_EDGE` | 价格不在窗口 |
| `STRATEGY_ORDER` | 下单结果 `outcome: filled / not_filled` |
| `STRATEGY_LIVE_ARM` | 直播监控已武装 |
| `STRATEGY_LIVE_SIGNAL` | 直播价触发 |
| `FINAL` | 聚合器确认终局（在 aggregator 日志） |
| `WATCH_ADD` | 市场加入 watchlist |
| `REDEEM_AUTO` / `REDEEM_OK` / `REDEEM_FAIL` | 自动/成功/失败结算 |

---

## 7. 常见问题

**Q：Dashboard 打不开？**  
确认 bot 在运行、`dashboard_enabled: true`，且浏览器对 localhost 不走代理。

**Q：健康检查 geoblock 失败？**  
开启 Shadowrocket/Clash，确认 `1082`（或你配置的端口）在监听。

**Q：队名对不上、日志出现 UNMAPPED？**  
在 SQLite `team_aliases` 表添加别名映射，见 README「队名映射」。

| `UNMAPPED` | 无法从队名匹配市场 |

**Q：结算按钮灰色或提示未启用？**  
确认 `mode: live`、`FUNDER`/`PK` 已配置，且已 `pip install -e ".[live]"`。

**Q：结算交易失败？**  
检查 EOA 是否有 POL gas；RPC 是否可用（可改 `polygon_rpc_url`）。

---

## 8. 相关文档

- [README.md](../README.md) — 项目概览与快速开始  
- [LOCAL_MIGRATION.zh.md](./LOCAL_MIGRATION.zh.md) — 从服务器迁移到本机  
- [config.example.yaml](../config.example.yaml) — 配置模板  
