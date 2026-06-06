# 迁移到本机运行

本压缩包为**源码 + 配置模板**，已剔除：虚拟环境、运行日志、SQLite 数据库、Python 缓存。私钥与 API Key **不会**打进包，需你在本机单独配置。

## 1. 拷贝与解压

```bash
# 将 polymarket-settlement-arb-*.tar.gz 拷到本机后：
tar xzf polymarket-settlement-arb-*.tar.gz
cd polymarket-settlement-arb
```

## 2. Python 环境（建议 3.11+）

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
# 需要真金下单时：
pip install -e ".[live]"
```

## 3. 配置

| 文件 | 用途 |
|------|------|
| `config.yaml` / `config.local.yaml` | 本机默认：`local_cn` + `127.0.0.1:1080` 代理 |
| `config.london.yaml` | 伦敦 VPS 参考（直连） |
| `.env.example` | 复制为 `.env`，填 API Key / 代理 / CLOB 凭证 |

```bash
cp .env.example polymarket-arb.env
# 编辑 polymarket-arb.env：PK、FUNDER（live）；CLOB 三件套可留空由 bot derive
# 启动时会自动加载 polymarket-arb.env，无需手动 source
```

大陆本地 `config.local.yaml` 默认代理 `127.0.0.1:1080`，请与本机 Clash/V2Ray 端口一致。

## 4. 目录（首次运行自动写入）

```bash
mkdir -p logs data
```

- 日志：`logs/arb.jsonl`
- 状态库：`data/arb.db`（watchlist、成交记录等，新环境为空库）

## 5. 启动前检查

```bash
source .venv/bin/activate
python -c "
import asyncio
from src.config import load_config
from src.health import run_health_checks, critical_failures
from src.net.proxy import ProxyTransport

async def main():
    cfg = load_config('config.yaml')
    r = await run_health_checks(cfg, ProxyTransport(cfg.proxy))
    for x in r:
        print(x.name, x.ok, x.detail)
    print('critical:', [x.name for x in critical_failures(r)])

asyncio.run(main())
"
```

`geoblock` 必须为 `ok=True`，否则 CLOB 会 403。

## 6. 运行

```bash
python -m src.main
# 或: python -m src.main --config config.yaml
```

验证无误后，可将 `mode` 改为 `live`（建议仍走代理）。

完整启停、Dashboard、配置与成交错过说明见 **[docs/USER_GUIDE.zh.md](USER_GUIDE.zh.md)**。

## 7. 从伦敦服务器单独带走的内容

**不要**从 tar 包里找私钥。请用安全方式（U 盘、密码管理器、加密通道）从服务器拷贝：

- `/etc/polymarket-arb.env` → 本机项目根目录 `.env` 或系统环境变量
- 各数据源 API Key（若只在 env 里）

服务器上可停止服务以免重复下单：

```bash
sudo systemctl stop polymarket-arb
sudo systemctl disable polymarket-arb   # 可选
```

## 8. 常见问题

- **403 Trading restricted**：出口 IP 被 geoblock，换住宅/非云 SOCKS5。
- **paper 无成交**：检查 `STRATEGY_ORDER` 的 `detail` 是否为 `geoblocked` / `live_paused`。
- **覆盖率脚本**：`python scripts/coverage_test.py --config config.local.yaml`
