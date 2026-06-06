"""FastAPI Dashboard 服务：WebSocket 推送 + 翻页 REST。"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.dashboard.hub import WATCHLIST_PAGE_SIZE, DashboardHub

logger = logging.getLogger("arb.dashboard")

STATIC_DIR = Path(__file__).parent / "static"


class RedeemBody(BaseModel):
    """手动结算请求体。"""

    condition_id: str = ""
    winners_only: bool = False
    all_redeemable: bool = False


def create_app(hub: DashboardHub) -> FastAPI:
    app = FastAPI(title="Polymarket Arb Dashboard")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/watchlist")
    async def api_watchlist(
        page: int = 1, page_size: int = WATCHLIST_PAGE_SIZE
    ) -> dict[str, Any]:
        return await asyncio.to_thread(hub.build_watchlist_page, page, page_size)

    @app.get("/api/history")
    async def api_history(page: int = 1, page_size: int = 5) -> dict[str, Any]:
        def _fetch() -> dict[str, Any]:
            items, total = hub._app.store.list_merged_history(page, page_size)
            return {"items": items, "total": total, "page": page, "page_size": page_size}

        return await asyncio.to_thread(_fetch)

    def _script_paths() -> tuple[Path, Path, Path, str]:
        root = hub._app.cfg.project_root
        stop_sh = root / "scripts" / "stop_bot.sh"
        start_sh = root / "scripts" / "start_bot.sh"
        config = getattr(hub._app, "_config_path", "config.yaml")
        return root, stop_sh, start_sh, config

    @app.get("/api/bot/status")
    async def api_bot_status() -> dict[str, Any]:
        """查询 bot 进程是否在运行。"""
        root, _, _, _ = _script_paths()
        pid_file = root / "logs" / "bot.pid"

        def _check() -> dict[str, Any]:
            if not pid_file.is_file():
                return {"running": False, "pid": None}
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                return {"running": False, "pid": None}
            import os

            try:
                os.kill(pid, 0)
                return {"running": True, "pid": pid}
            except OSError:
                return {"running": False, "pid": pid}

        return await asyncio.to_thread(_check)

    @app.get("/api/positions")
    async def api_positions() -> dict[str, Any]:
        """可结算持仓列表（含价格百分比）。"""
        app_ref = hub._app

        async def _fetch() -> dict[str, Any]:
            items = await app_ref.redeem.list_settlement_candidates(winner_only=False)
            return {
                "items": items,
                "enabled": app_ref.redeem.enabled(),
                "auto_redeem": app_ref.cfg.auto_redeem_enabled,
                "threshold_pct": round(app_ref.cfg.redeem_price_threshold * 100, 1),
            }

        return await _fetch()

    @app.post("/api/redeem")
    async def api_redeem(body: RedeemBody) -> dict[str, Any]:
        """手动结算：指定 condition_id，或 winners_only / all_redeemable。"""
        app_ref = hub._app
        condition_id = body.condition_id.strip()
        winners_only = body.winners_only
        all_redeemable = body.all_redeemable

        async def _run() -> dict[str, Any]:
            if not app_ref.redeem.enabled():
                return {"ok": False, "error": "live 模式未配置 FUNDER/PK，或未安装 web3"}
            if condition_id:
                positions = await app_ref.redeem.fetch_positions()
                pos = next((p for p in positions if p.condition_id == condition_id), None)
                outcome = await asyncio.to_thread(
                    app_ref.redeem.redeem_condition,
                    condition_id,
                    pos=pos,
                    trigger="manual",
                    winner_only=False,
                )
                return {
                    "ok": outcome.ok,
                    "results": [
                        {
                            "condition_id": outcome.condition_id,
                            "title": outcome.title,
                            "tx_hash": outcome.tx_hash,
                            "detail": outcome.detail,
                            "usdc_gained": outcome.usdc_gained,
                        }
                    ],
                }
            if all_redeemable or winners_only:
                results = await app_ref.redeem.redeem_all_manual(winners_only=winners_only)
                return {
                    "ok": any(r.ok for r in results),
                    "results": [
                        {
                            "condition_id": r.condition_id,
                            "title": r.title,
                            "tx_hash": r.tx_hash,
                            "detail": r.detail,
                            "usdc_gained": r.usdc_gained,
                            "ok": r.ok,
                        }
                        for r in results
                    ],
                }
            return {"ok": False, "error": "请传 condition_id 或 all_redeemable/winners_only"}

        return await _run()

    @app.post("/api/stop")
    async def api_stop() -> dict[str, str]:
        """停止 bot（Dashboard 会随主进程一起退出）。"""
        root, stop_sh, _, _ = _script_paths()

        async def _stop() -> None:
            await asyncio.sleep(0.4)
            if stop_sh.is_file():
                subprocess.Popen(["/bin/bash", str(stop_sh)], cwd=str(root))  # noqa: S603

        asyncio.create_task(_stop())
        return {"status": "stopping"}

    @app.post("/api/start")
    async def api_start() -> dict[str, str]:
        """启动 bot（若已在运行则跳过）。"""
        root, _, start_sh, config = _script_paths()

        async def _start() -> None:
            await asyncio.sleep(0.2)
            if start_sh.is_file():
                subprocess.Popen(  # noqa: S603
                    ["/bin/bash", str(start_sh), str(config)],
                    cwd=str(root),
                )

        asyncio.create_task(_start())
        return {"status": "starting"}

    @app.post("/api/restart")
    async def api_restart() -> dict[str, str]:
        """重启 bot（调用 stop + start 脚本）。"""
        root, stop_sh, start_sh, config = _script_paths()

        async def _restart() -> None:
            await asyncio.sleep(0.5)
            if stop_sh.is_file():
                subprocess.Popen(["/bin/bash", str(stop_sh)], cwd=str(root))  # noqa: S603
            await asyncio.sleep(2)
            if start_sh.is_file():
                subprocess.Popen(  # noqa: S603
                    ["/bin/bash", str(start_sh), str(config)],
                    cwd=str(root),
                )

        asyncio.create_task(_restart())
        return {"status": "restarting"}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await hub.register_client(ws)
        try:
            await ws.send_json(await asyncio.to_thread(hub.build_snapshot))
            while True:
                # 客户端翻页走 WebSocket，避免阻塞主事件循环
                raw = await ws.receive_text()
                if raw.startswith("{"):
                    import json

                    msg = json.loads(raw)
                    if msg.get("type") == "watchlist.page":
                        page = int(msg.get("page", 1))
                        ps = int(msg.get("page_size", WATCHLIST_PAGE_SIZE))
                        data = await asyncio.to_thread(hub.build_watchlist_page, page, ps)
                        await ws.send_json({"type": "watchlist.page", "data": data})
                    elif msg.get("type") == "history.page":
                        page = int(msg.get("page", 1))

                        def _hist() -> dict[str, Any]:
                            items, total = hub._app.store.list_merged_history(page, 5)
                            return {
                                "items": items,
                                "total": total,
                                "page": page,
                                "page_size": 5,
                            }

                        await ws.send_json(
                            {"type": "history.page", "data": await asyncio.to_thread(_hist)}
                        )
        except WebSocketDisconnect:
            pass
        finally:
            await hub.unregister_client(ws)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


async def run_dashboard_server(hub: DashboardHub, host: str, port: int) -> None:
    """在 asyncio 中运行 uvicorn（与 bot 共享事件循环）。"""
    app = create_app(hub)
    # loop="none"：嵌入已有 asyncio 事件循环（uvicorn 0.49+）
    config = uvicorn.Config(app, host=host, port=port, log_level="info", loop="none")
    server = uvicorn.Server(config)
    logger.info("Dashboard 监听 http://%s:%d", host, port)
    await server.serve()
