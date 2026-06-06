"""
7×24 主入口：Gamma 同步 → 反向匹配 → 多源赛果 → 信号 → 分级下单。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from src.config import AppConfig, load_config
from src.engine.ladder_executor import LadderExecutor
from src.engine.risk import RiskManager
from src.engine.signals import SignalEngine
from src.logging_setup import log_event, setup_logging
from src.matcher.reverse_matcher import ReverseMatcher
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter, MultiWindowRateLimiter
from src.pm.clob_ws import ClobOrderbookFeed
from src.pm.gamma_sync import GammaSync
from src.sports.aggregator import FixtureAggregator
from src.sports.api_football import ApiFootballProvider
from src.sports.balldontlie import BallDontLieProvider
from src.sports.espn_nba import EspnNbaProvider
from src.sports.espn_soccer import EspnSoccerProvider
from src.sports.football_data import FootballDataProvider
from src.sports.openligadb import OpenLigaDbProvider
from src.sports.thesportsdb import TheSportsDbProvider
from src.store.sqlite import Store

logger = logging.getLogger("arb")


class ArbApp:
    """应用上下文，组装各模块。"""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.proxy = ProxyTransport(cfg.proxy)
        self.store = Store(cfg.resolve_path(cfg.db_path))
        self.matcher = ReverseMatcher(self.store)
        self.aggregator = FixtureAggregator(cfg)
        self.books = ClobOrderbookFeed(cfg, self.proxy)
        self.risk = RiskManager(cfg, self.store)
        self.ladder = LadderExecutor(cfg, self.store, self.risk, self.proxy)
        self.signals = SignalEngine(
            cfg, self.store, self.matcher, self.aggregator, self.books, self.ladder
        )
        self.gamma = GammaSync(cfg, self.proxy, self.store)

        # ---- 各数据源独立限流器（分开计数，互不影响）----
        self.limiters: dict[str, Any] = {
            "football_data": AsyncRateLimiter(cfg.rate_football_data_per_min, 60.0, "football_data"),
            # API-Football：10/分钟 + 每日配额双窗口
            "api_football": MultiWindowRateLimiter(
                [(cfg.rate_api_football_per_min, 60.0), (cfg.api_football_daily_quota, 86400.0)],
                name="api_football",
            ),
            "thesportsdb": AsyncRateLimiter(cfg.rate_thesportsdb_per_min, 60.0, "thesportsdb"),
            "balldontlie": AsyncRateLimiter(cfg.rate_balldontlie_per_min, 60.0, "balldontlie"),
            "espn_soccer": AsyncRateLimiter(cfg.rate_espn_per_min, 60.0, "espn_soccer"),
            "espn_nba": AsyncRateLimiter(cfg.rate_espn_per_min, 60.0, "espn_nba"),
            "openligadb": AsyncRateLimiter(cfg.rate_openligadb_per_min, 60.0, "openligadb"),
        }

        # ---- 足球数据源（全部接入，各自限流）----
        self.espn_soccer = EspnSoccerProvider(cfg, self.proxy, self.store, self.limiters["espn_soccer"])
        self.openligadb = OpenLigaDbProvider(cfg, self.proxy, self.store, self.limiters["openligadb"])
        self.football_data = FootballDataProvider(cfg, self.proxy, self.store, self.limiters["football_data"])
        self.api_football = ApiFootballProvider(cfg, self.proxy, self.store, self.limiters["api_football"])
        self.thesportsdb = TheSportsDbProvider(cfg, self.proxy, self.store, self.limiters["thesportsdb"])

        # ---- NBA 数据源（nba_api 已移除，ESPN + BallDontLie 兜底）----
        self.espn_nba = EspnNbaProvider(self.proxy, self.store, self.limiters["espn_nba"])
        self.balldontlie = BallDontLieProvider(cfg, self.proxy, self.store, self.limiters["balldontlie"])

        # 按运动归类的数据源列表（TheSportsDB 同时覆盖足球+篮球）
        self.football_sources = [
            self.espn_soccer,
            self.openligadb,
            self.football_data,
            self.api_football,
            self.thesportsdb,
        ]
        self.nba_sources = [
            self.espn_nba,
            self.balldontlie,
        ]

        self._stop = asyncio.Event()
        self._has_live_fixtures = False
        self._config_path = "config.yaml"
        self._dashboard_hub = None
        self._last_watchlist_count = 0

    async def _fetch_all_fixtures(self) -> list:
        """汇总所有已启用数据源的最新赛事更新（各源内部已限流）。"""
        all_fixtures: list = []
        if "football" in self.cfg.sports:
            for src in self.football_sources:
                try:
                    all_fixtures.extend(await src.fetch_updates())
                except Exception as e:
                    logger.debug("数据源 %s 拉取异常: %s", getattr(src, "source_id", src), e)
        if "nba" in self.cfg.sports:
            for src in self.nba_sources:
                try:
                    all_fixtures.extend(await src.fetch_updates())
                except Exception as e:
                    logger.debug("数据源 %s 拉取异常: %s", getattr(src, "source_id", src), e)
        return all_fixtures

    async def start(self) -> None:
        """注册回调并启动后台任务。"""
        # 重启后清除历史 live_paused（避免昨日连续失败锁死）
        self.risk.reset_pause()

        # 终局信号 + 盘口高频回调（直播价格触发）
        self.aggregator.on_final(self.signals.on_final)
        self.books.on_update(self.signals.on_book_update)
        self.books.on_update(self._on_book_dashboard)

        tasks = [
            asyncio.create_task(self._gamma_sync_loop(), name="gamma_sync"),
            asyncio.create_task(self._sports_poll_loop(), name="sports_poll"),
            asyncio.create_task(self._clob_poll_loop(), name="clob_poll"),
            asyncio.create_task(self._health_loop(), name="health"),
        ]
        # WS 与 REST 轮询并行，WS 失败时 REST 仍可用
        tasks.append(asyncio.create_task(self._clob_ws_loop(), name="clob_ws"))

        if self.cfg.dashboard_enabled:
            from src.dashboard.bus import DashboardBus, set_bus
            from src.dashboard.hub import DashboardHub
            from src.dashboard.log_handler import DashboardLogHandler
            from src.dashboard.server import run_dashboard_server

            bus = DashboardBus()
            set_bus(bus)
            bus.start()
            hub = DashboardHub(self)
            self._dashboard_hub = hub
            bus.subscribe(hub.handle_event)

            root_logger = logging.getLogger("arb")
            dash_handler = DashboardLogHandler()
            dash_handler.setLevel(logging.INFO)
            root_logger.addHandler(dash_handler)

            tasks.append(
                asyncio.create_task(
                    run_dashboard_server(hub, self.cfg.dashboard_host, self.cfg.dashboard_port),
                    name="dashboard",
                )
            )
            from src.dashboard.bus import emit_event

            emit_event("status.updated", {})
            self._emit_initial_source_health()

        log_event(
            logger,
            "bot 已启动",
            environment=self.cfg.environment,
            mode=self.cfg.mode,
            proxy=self.cfg.proxy.enabled,
        )

        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.proxy.aclose()
        self.store.close()

    def request_stop(self) -> None:
        self._stop.set()

    def _emit_initial_source_health(self) -> None:
        """Dashboard 启动时推送各数据源初始状态（未配置 Key 的显示为 disabled）。"""
        from src.dashboard.bus import emit_event

        providers = [
            self.espn_soccer,
            self.openligadb,
            self.football_data,
            self.api_football,
            self.thesportsdb,
            self.espn_nba,
            self.balldontlie,
        ]
        for src in providers:
            sid = src.source_id
            if hasattr(src, "enabled") and not src.enabled:
                emit_event(
                    "source.health",
                    {
                        "id": sid,
                        "ok": None,
                        "status": "disabled",
                        "last_ts": 0,
                        "error": "未配置 API Key，已跳过",
                    },
                )
                continue
            row = next(
                (r for r in self.store.list_source_health() if r["source_id"] == sid),
                None,
            )
            if row:
                ok = bool(row["last_ok_ts"]) and not row["last_error"]
                emit_event(
                    "source.health",
                    {
                        "id": sid,
                        "ok": ok,
                        "status": "ok" if ok else "error",
                        "last_ts": row["last_ok_ts"] or 0,
                        "error": row["last_error"] or "",
                    },
                )
            else:
                emit_event(
                    "source.health",
                    {
                        "id": sid,
                        "ok": None,
                        "status": "pending",
                        "last_ts": 0,
                        "error": "等待首次拉取",
                    },
                )

    async def _on_book_dashboard(self, token_id: str, snap: Any) -> None:
        """盘口更新时推送 dashboard（与 signals 并行）。"""
        from src.dashboard.bus import emit_event

        emit_event(
            "book.updated",
            {
                "token_id": token_id,
                "best_ask": snap.best_ask,
                "best_ask_size": snap.best_ask_size,
            },
        )

    def _maybe_emit_watchlist_changed(self) -> None:
        wl = self.store.list_active_watchlist()
        count = len(wl)
        if count != self._last_watchlist_count:
            self._last_watchlist_count = count
            from src.dashboard.bus import emit_event

            emit_event("watchlist.changed", {"page": 1})

    async def _gamma_sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                markets = await self.gamma.sync_markets()
                all_fixtures = await self._fetch_all_fixtures()

                for m in markets:
                    if m.watch_state == "watching":
                        continue
                    key = self.matcher.try_match_from_fixtures(
                        m.market_id,
                        m.sport,
                        m.team_a,
                        m.team_b,
                        all_fixtures,
                    )
                    if not key:
                        self.matcher.register_market(
                            m.market_id,
                            m.sport,
                            m.team_a,
                            m.team_b,
                        ) if m.team_a and m.team_b else None
                        if not key and not (m.team_a and m.team_b):
                            log_event(logger, "UNMAPPED", market_id=m.market_id, question=m.question)

                # 注意：不再一次性订阅全部 watchlist token。
                # 仅在比赛进入早进场阶段（足球 80 分钟后/NBA 第四节）时，
                # 由 SignalEngine 动态订阅该市场两侧 token 做高频盘口轮询。
                self._maybe_emit_watchlist_changed()
            except Exception as e:
                logger.exception("gamma_sync 异常: %s", e)
            await asyncio.sleep(self.cfg.gamma_sync_interval_sec)

    async def _sports_poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self._fetch_all_fixtures()

                # 摄入：检测终局 + 更新各场实时进度
                events = self.aggregator.ingest(updates)
                await self.aggregator.emit(events)

                # 价格驱动早进场：足球 80 分钟后武装并盯盘口；NBA 默认等终局。
                self.signals.arm_live_from_fixtures(self.aggregator.live_fixtures())

                # 存在已武装市场时进入高频节奏，否则空闲低频（省额度）
                self._has_live_fixtures = self.signals.has_armed()
                interval = (
                    self.cfg.sports_poll_live_sec
                    if self._has_live_fixtures
                    else self.cfg.sports_poll_idle_sec
                )
            except Exception as e:
                logger.exception("sports_poll 异常: %s", e)
                interval = self.cfg.sports_poll_idle_sec
            await asyncio.sleep(interval)

    async def _clob_poll_loop(self) -> None:
        try:
            await self.books.poll_loop(interval=self.cfg.clob_eligible_poll_sec)
        except asyncio.CancelledError:
            pass

    async def _clob_ws_loop(self) -> None:
        try:
            await self.books.ws_loop()
        except asyncio.CancelledError:
            pass

    async def _health_loop(self) -> None:
        from src.health import critical_failures, optional_failures, run_health_checks

        while not self._stop.is_set():
            try:
                # 各数据源限流计数快照（分开统计，便于核对 10/10/30 配额消耗）
                log_event(
                    logger,
                    "RATE_LIMIT_STATS",
                    sources={name: lim.stats() for name, lim in self.limiters.items()},
                    armed_markets=len(self.signals._armed),
                )
                # watchlist 快照：方便复盘当前在盯哪些比赛、分运动数量、已武装直播场次
                wl = self.store.list_active_watchlist()
                by_sport: dict[str, int] = {}
                for r in wl:
                    by_sport[r["sport"]] = by_sport.get(r["sport"], 0) + 1
                log_event(
                    logger,
                    "WATCHLIST_SNAPSHOT",
                    total=len(wl),
                    by_sport=by_sport,
                    armed=len(self.signals._armed),
                    armed_markets=sorted(self.signals._armed.keys()),
                )
                results = await run_health_checks(self.cfg, ProxyTransport(self.cfg.proxy))
                crit = critical_failures(results)
                opt = optional_failures(results)
                if opt:
                    log_event(
                        logger,
                        "healthcheck 可选源失败",
                        failed=[f"{r.name}:{r.detail}" for r in opt],
                    )
                if crit:
                    log_event(
                        logger,
                        "healthcheck 关键项失败",
                        failed=[f"{r.name}:{r.detail}" for r in crit],
                    )
                # geoblock 状态单独维护
                geo = next((r for r in results if r.name == "geoblock"), None)
                if geo is not None:
                    self.risk.set_geoblocked(not geo.ok, geo.detail)
                # 仅 gamma/clob 挂掉时暂停 live
                clob_down = any(r.name in ("gamma", "clob") for r in crit)
                if self.cfg.mode == "live" and clob_down:
                    self.risk._live_paused = True
                elif self.cfg.mode == "live" and not crit:
                    self.risk.reset_pause()

                from src.dashboard.bus import emit_event

                critical_items = []
                for r in results:
                    if r.name in ("gamma", "clob", "geoblock", "espn_nba", "espn_soccer", "openligadb"):
                        critical_items.append(
                            {
                                "id": r.name,
                                "ok": r.ok,
                                "last_ts": __import__("time").time(),
                                "error": r.detail if not r.ok else "",
                            }
                        )
                emit_event("health.critical", {"items": critical_items})

                if self.cfg.mode == "live":
                    await self._probe_payment_api()

            except Exception as e:
                logger.warning("health 检查异常: %s", e)
            await asyncio.sleep(self.cfg.health_interval_sec)

    async def _probe_payment_api(self) -> None:
        """live 模式下探测 CLOB 支付 API 可用性。"""
        import time

        from src.dashboard.bus import emit_event

        detail = ""
        ok = False
        try:
            if self.ladder._init_live_client():
                bal = self.ladder._get_available_usdc()
                ok = bal is not None
                detail = f"usdc={bal:.2f}" if bal is not None else "balance_ok"
            else:
                detail = "client_not_ready"
        except Exception as e:
            detail = str(e)[:200]
        payload = {"ok": ok, "detail": detail, "last_ts": time.time()}
        if self._dashboard_hub:
            self._dashboard_hub._payment_api = payload
        emit_event("payment.api", payload)


async def run_app(config_path: str) -> None:
    cfg = load_config(config_path)
    log_path = cfg.resolve_path(cfg.log_path)
    setup_logging(log_path, logging.INFO)

    # 启动前健康检查
    proxy = ProxyTransport(cfg.proxy)
    from src.health import critical_failures, optional_failures, run_health_checks

    results = await run_health_checks(cfg, proxy)
    for r in optional_failures(results):
        logger.warning("启动前可选检查失败 %s: %s", r.name, r.detail)
    for r in critical_failures(results):
        logger.error("启动前关键检查失败 %s: %s", r.name, r.detail)
    geo = next((r for r in results if r.name == "geoblock"), None)
    if geo and not geo.ok:
        logger.error(
            "当前出口 IP 被 Polymarket 地区限制 (%s)。"
            "请在 config.yaml 启用 proxy（本机默认 127.0.0.1:1080），"
            "或在 polymarket-arb.env 设置 SOCKS5_PROXY 后重启。",
            geo.detail,
        )
    if cfg.mode == "live" and critical_failures(results):
        logger.error("live 模式要求 gamma/clob/geoblock 关键检查通过")
        if cfg.environment == "local_cn":
            logger.error("大陆环境请确认 127.0.0.1:1080 代理已开启")

    app = ArbApp(cfg)
    app._config_path = str(config_path)
    if geo is not None:
        app.risk.set_geoblocked(not geo.ok, geo.detail)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass

    await app.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket 结算延迟套利 bot")
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="配置文件路径",
    )
    args = parser.parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run_app(str(config_path)))


if __name__ == "__main__":
    main()
