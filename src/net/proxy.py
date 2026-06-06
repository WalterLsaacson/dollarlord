"""统一代理层：HTTP / WebSocket / requests 环境。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import httpx

from src.config import ProxyConfig


class ProxyTransport:
    """封装出站代理，供各模块复用。"""

    def __init__(self, proxy: ProxyConfig) -> None:
        self.proxy = proxy
        self._httpx_client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self.proxy.enabled

    def httpx_proxy_url(self) -> str | None:
        """httpx 优先 SOCKS5，回退 HTTP。"""
        if not self.proxy.enabled:
            return None
        if self.proxy.socks5_url:
            return self.proxy.socks5_url
        return self.proxy.http_url or None

    def requests_proxies(self) -> dict[str, str] | None:
        """requests / nba_api 用代理字典。"""
        if not self.proxy.enabled:
            return None
        url = self.proxy.socks5_url or self.proxy.http_url
        if not url:
            return None
        # socks5h 让 DNS 也走代理
        if url.startswith("socks5://"):
            url = url.replace("socks5://", "socks5h://", 1)
        return {"http": url, "https": url}

    def websocket_proxy(self) -> str | None:
        """websockets + python-socks 用 SOCKS5 URL。"""
        if not self.proxy.enabled:
            return None
        return self.proxy.socks5_url or None

    async def get_httpx_client(self) -> httpx.AsyncClient:
        """复用异步 HTTP 客户端。"""
        if self._httpx_client is None or self._httpx_client.is_closed:
            self._httpx_client = httpx.AsyncClient(
                proxy=self.httpx_proxy_url(),
                timeout=httpx.Timeout(30.0, connect=15.0),
                follow_redirects=True,
            )
        return self._httpx_client

    async def aclose(self) -> None:
        if self._httpx_client and not self._httpx_client.is_closed:
            await self._httpx_client.aclose()
            self._httpx_client = None

    @contextmanager
    def requests_env(self) -> Iterator[None]:
        """临时设置环境变量供 nba_api 等库使用。"""
        if not self.proxy.enabled:
            yield
            return
        proxies = self.requests_proxies()
        if not proxies:
            yield
            return
        url = proxies.get("https") or proxies.get("http")
        old: dict[str, str | None] = {
            "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
            "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
            "ALL_PROXY": os.environ.get("ALL_PROXY"),
        }
        try:
            os.environ["HTTP_PROXY"] = url or ""
            os.environ["HTTPS_PROXY"] = url or ""
            os.environ["ALL_PROXY"] = url or ""
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def sync_get(self, url: str, **kwargs: Any) -> httpx.Response:
        """同步 GET（nba_api 等场景外的简易调用）。"""
        with httpx.Client(proxy=self.httpx_proxy_url(), timeout=30.0) as client:
            return client.get(url, **kwargs)
