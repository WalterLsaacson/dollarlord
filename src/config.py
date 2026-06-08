"""配置加载（YAML + 环境变量）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class ProxyConfig(BaseModel):
    """代理配置（大陆测试用）。"""

    enabled: bool = False
    socks5_url: str = "socks5://127.0.0.1:1082"
    http_url: str = "http://127.0.0.1:1082"


class AppConfig(BaseModel):
    """应用主配置。"""

    environment: Literal["local_cn", "london"] = "local_cn"
    mode: Literal["paper", "live"] = "paper"
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)

    entry_max_price: float = 0.99
    max_round_notional_usd: float = 50.0
    order_ladder_usd: list[float] | None = None
    min_ladder_step_usd: float = 5.0
    market_cooldown_sec: int = 3600

    # ---- 价格驱动早进场策略 ----
    early_entry_enabled: bool = True
    # 买入价下限（与 entry_max_price 构成套利窗口，默认 0.60~0.99）
    early_entry_price: float = 0.60
    # 足球：开赛多少分钟后才允许进入轮询/下单（之前悬念较大，不参与）
    football_min_elapsed_min: int = 80
    # 足球：净胜球分差 > 此值时立即武装买入领先方，不必等 80 分钟（大比分基本锁定胜负）
    football_blowout_lead: int = 3
    # 足球：拿不到真实比赛分钟时，用开赛后的墙钟分钟兜底（含中场休息缓冲）
    football_fallback_wallclock_min: int = 95
    # NBA：默认不等第四节直播价，仅终局赛果触发下单（见 nba_early_entry_enabled）
    nba_min_period: int = 4
    nba_early_entry_enabled: bool = False

    gamma_sync_interval_sec: int = 900
    sports_poll_live_sec: float = 2.0
    sports_poll_idle_sec: float = 60.0
    # 有资格比赛时盘口高频轮询间隔（秒）
    clob_eligible_poll_sec: float = 1.5
    conflict_window_sec: int = 120
    health_interval_sec: int = 300

    # ---- 各数据源每分钟最大请求数（分开计数限流） ----
    rate_football_data_per_min: int = 10
    rate_api_football_per_min: int = 10
    rate_thesportsdb_per_min: int = 30
    rate_balldontlie_per_min: int = 5
    # ESPN 三个运动端点共享此限速（soccer + NBA + NFL 合计不超过该值/分钟）
    rate_espn_per_min: int = 60
    rate_openligadb_per_min: int = 15
    rate_nba_api_per_min: int = 10
    # MLB / NHL 官方免费 API（各自独立限速）
    rate_mlb_per_min: int = 30
    rate_nhl_per_min: int = 30
    # API-Football 免费层每日配额保护（10/min 之外再叠加每日上限）
    api_football_daily_quota: int = 95

    # ---- 各数据源 API key（留空则该源自动禁用；可用环境变量覆盖）----
    football_data_api_key: str = ""
    api_football_key: str = ""
    thesportsdb_key: str = "123"  # 官方免费公共 key
    balldontlie_key: str = ""
    # API-Football 主机（api-sports 直连或 RapidAPI 代理）
    api_football_host: str = "v3.football.api-sports.io"
    # TheSportsDB 关注的运动类型（用于按日赛程查询）
    thesportsdb_sports: list[str] = Field(default_factory=lambda: ["Soccer", "Basketball"])
    # football-data.org 关注的竞赛代码（免费层覆盖：PL/PD/BL1/SA/FL1/CL/WC 等）
    football_data_competitions: list[str] = Field(
        default_factory=lambda: ["PL", "PD", "BL1", "SA", "FL1", "DED", "PPL", "CL", "WC", "EC"]
    )

    sports: list[str] = Field(default_factory=lambda: ["football", "nba"])
    espn_soccer_leagues: list[str] = Field(
        default_factory=lambda: [
            "eng.1",
            "esp.1",
            "ger.1",
            "usa.1",
            "uefa.champions",
            "uefa.europa",
        ]
    )
    openligadb_leagues: list[str] = Field(default_factory=lambda: ["bl1", "ucl"])

    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    db_path: str = "data/arb.db"
    log_path: str = "logs/arb.jsonl"
    webhook_url: str = ""

    max_daily_trades: int = 100
    max_consecutive_failures: int = 5

    # ---- 持仓结算（redeem）----
    auto_redeem_enabled: bool = True
    redeem_poll_sec: int = 120
    # curPrice ≥ 此值视为胜方「100%」，触发自动结算
    redeem_price_threshold: float = 1.0
    # Dashboard Watchlist：开赛后仍展示的小时数，超时移除
    watchlist_grace_hours: float = 2.0
    polygon_rpc_url: str = "https://polygon-rpc.com"
    history_page_size: int = 10

    # ---- 可视化 Dashboard（内嵌 Web，事件推送）----
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787

    # 运行时根目录（加载配置后设置）
    project_root: Path = Field(default_factory=Path.cwd)

    def resolve_path(self, rel: str) -> Path:
        """相对路径转绝对路径。"""
        p = Path(rel)
        if p.is_absolute():
            return p
        return self.project_root / p

    def get_order_ladder(self) -> list[float]:
        """生成分级下单金额阶梯。"""
        if self.order_ladder_usd:
            return list(self.order_ladder_usd)
        ladder: list[float] = []
        amount = float(self.max_round_notional_usd)
        while amount >= self.min_ladder_step_usd:
            ladder.append(round(amount, 2))
            amount /= 2
        if not ladder:
            ladder = [self.min_ladder_step_usd]
        return ladder


def _load_env_file(path: Path) -> None:
    """解析 KEY=VALUE 行写入 os.environ（不覆盖已有变量）。"""
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def load_dotenv_files(project_root: Path) -> None:
    """加载项目根 .env / polymarket-arb.env（本机无需手动 source）。"""
    for name in (".env", "polymarket-arb.env"):
        env_path = project_root / name
        if env_path.is_file():
            _load_env_file(env_path)


def load_config(path: str | Path) -> AppConfig:
    """从 YAML 文件加载配置。"""
    path = Path(path)
    load_dotenv_files(path.parent.resolve())
    with path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    cfg = AppConfig.model_validate(raw)
    cfg.project_root = path.parent.resolve()

    # API key 优先从环境变量读取（YAML 未填时），便于把密钥放进 /etc/polymarket-arb.env
    env_map = {
        "football_data_api_key": "FOOTBALL_DATA_API_KEY",
        "api_football_key": "API_FOOTBALL_KEY",
        "balldontlie_key": "BALLDONTLIE_KEY",
    }
    for field, env_name in env_map.items():
        if not getattr(cfg, field):
            val = os.environ.get(env_name)
            if val:
                setattr(cfg, field, val)
    # TheSportsDB 允许环境变量覆盖默认免费 key
    tsdb = os.environ.get("THESPORTSDB_KEY")
    if tsdb:
        cfg.thesportsdb_key = tsdb
    # 环境变量 SOCKS5/HTTP 代理：用于绕过 Polymarket geoblock（云主机 GB IP 常被拦）
    socks = os.environ.get("SOCKS5_PROXY") or os.environ.get("ALL_PROXY")
    http_p = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if socks:
        cfg.proxy.enabled = True
        cfg.proxy.socks5_url = socks
    elif http_p:
        cfg.proxy.enabled = True
        cfg.proxy.http_url = http_p
    return cfg
