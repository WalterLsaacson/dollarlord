"""CLOB 盘口 WebSocket + REST 订单簿。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from src.config import AppConfig
from src.net.proxy import ProxyTransport

logger = logging.getLogger("arb.clob_ws")


@dataclass
class OrderbookSnapshot:
    """订单簿快照。"""

    token_id: str
    best_ask: float | None = None
    best_ask_size: float = 0.0
    asks: list[tuple[float, float]] = field(default_factory=list)  # price, size

    def available_notional(self, max_price: float) -> float:
        """在 max_price 以下的卖单名义总额（USD）。"""
        total = 0.0
        for price, size in self.asks:
            if price <= max_price:
                total += price * size
        return total


class ClobOrderbookFeed:
    """订阅 token 订单簿（REST 轮询 + 可选 WS）。"""

    def __init__(self, cfg: AppConfig, proxy: ProxyTransport) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self._books: dict[str, OrderbookSnapshot] = {}
        self._subscribed: set[str] = set()
        self._callbacks: list[Callable[[str, OrderbookSnapshot], Awaitable[None]]] = []

    def on_update(self, cb: Callable[[str, OrderbookSnapshot], Awaitable[None]]) -> None:
        self._callbacks.append(cb)

    def subscribe(self, token_id: str) -> None:
        self._subscribed.add(token_id)

    def unsubscribe(self, token_id: str) -> None:
        self._subscribed.discard(token_id)

    def replace_subscriptions(self, token_ids: set[str]) -> tuple[int, int]:
        """整体替换订阅集合（用于按比赛资格动态订阅）。

        返回 (新增数, 移除数)，便于上层做结构化日志。
        """
        current = set(self._subscribed)
        added = token_ids - current
        removed = current - token_ids
        for t in added:
            self._subscribed.add(t)
        for t in removed:
            self._subscribed.discard(t)
        return len(added), len(removed)

    def subscribed_tokens(self) -> set[str]:
        return set(self._subscribed)

    def get_book(self, token_id: str) -> OrderbookSnapshot | None:
        return self._books.get(token_id)

    async def fetch_book_rest(self, token_id: str) -> OrderbookSnapshot:
        """REST 拉取订单簿。"""
        client = await self.proxy.get_httpx_client()
        url = f"{self.cfg.clob_host}/book"
        resp = await client.get(url, params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        asks_raw = data.get("asks") or []
        asks: list[tuple[float, float]] = []
        for a in asks_raw:
            price = float(a.get("price", 0))
            size = float(a.get("size", 0))
            asks.append((price, size))
        asks.sort(key=lambda x: x[0])
        best_ask = asks[0][0] if asks else None
        best_size = asks[0][1] if asks else 0.0
        snap = OrderbookSnapshot(
            token_id=token_id,
            best_ask=best_ask,
            best_ask_size=best_size,
            asks=asks,
        )
        self._books[token_id] = snap
        return snap

    async def poll_loop(self, interval: float = 1.0) -> None:
        """轮询已订阅 token 的订单簿。"""
        while True:
            for token_id in list(self._subscribed):
                try:
                    snap = await self.fetch_book_rest(token_id)
                    for cb in self._callbacks:
                        await cb(token_id, snap)
                except Exception as e:
                    logger.debug("订单簿 %s 失败: %s", token_id[:16], e)
            await asyncio.sleep(interval)

    async def ws_loop(self) -> None:
        """WebSocket 订阅（带 SOCKS 代理）。"""
        import websockets
        from python_socks.async_.asyncio import Proxy

        proxy_url = self.proxy.websocket_proxy()
        backoff = 1.0
        while True:
            try:
                if proxy_url:
                    proxy = Proxy.from_url(proxy_url)
                    sock = await proxy.connect(
                        dest_host="ws-subscriptions-clob.polymarket.com",
                        dest_port=443,
                    )
                    async with websockets.connect(
                        self.cfg.clob_ws_url,
                        sock=sock,
                        server_hostname="ws-subscriptions-clob.polymarket.com",
                        ping_interval=20,
                    ) as ws:
                        await self._ws_subscribe_all(ws)
                        backoff = 1.0
                        await self._ws_read(ws)
                else:
                    async with websockets.connect(
                        self.cfg.clob_ws_url,
                        ping_interval=20,
                    ) as ws:
                        await self._ws_subscribe_all(ws)
                        backoff = 1.0
                        await self._ws_read(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("CLOB WS 断开: %s, %ds 后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _ws_subscribe_all(self, ws: Any) -> None:
        if not self._subscribed:
            return
        msg = {
            "assets_ids": list(self._subscribed),
            "type": "market",
        }
        await ws.send(json.dumps(msg))

    async def _ws_read(self, ws: Any) -> None:
        async for raw in ws:
            try:
                data = json.loads(raw)
                await self._handle_ws_message(data)
            except json.JSONDecodeError:
                continue

    async def _handle_ws_message(self, data: dict | list) -> None:
        """解析 WS 盘口更新（结构因版本可能变化，兼容 list/event）。"""
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            asset_id = ev.get("asset_id") or ev.get("assetId")
            if not asset_id or asset_id not in self._subscribed:
                continue
            asks = ev.get("asks") or ev.get("sell") or []
            parsed: list[tuple[float, float]] = []
            for a in asks:
                if isinstance(a, dict):
                    parsed.append((float(a.get("price", 0)), float(a.get("size", 0))))
                elif isinstance(a, (list, tuple)) and len(a) >= 2:
                    parsed.append((float(a[0]), float(a[1])))
            if parsed:
                parsed.sort(key=lambda x: x[0])
                snap = OrderbookSnapshot(
                    token_id=asset_id,
                    best_ask=parsed[0][0],
                    best_ask_size=parsed[0][1],
                    asks=parsed,
                )
                self._books[asset_id] = snap
                for cb in self._callbacks:
                    await cb(asset_id, snap)
