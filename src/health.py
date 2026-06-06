"""连通性自检（与主程序共用代理配置）。"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

from src.config import AppConfig, load_config
from src.net.proxy import ProxyTransport

logger = logging.getLogger("arb.health")

# 交易必需：任一失败则不应发 live 单（nba_api 等可选源失败不暂停交易）
CRITICAL_CHECKS = frozenset({"gamma", "clob", "geoblock"})

CHECK_URLS = {
    "gamma": "https://gamma-api.polymarket.com/markets?limit=1",
    "clob": "https://clob.polymarket.com/",
    "espn_nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "espn_soccer": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "openligadb": "https://api.openligadb.de/getmatchdata/bl1",
}

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


async def run_health_checks(cfg: AppConfig, proxy: ProxyTransport) -> list[CheckResult]:
    """执行端点连通检查。"""
    results: list[CheckResult] = []
    client = await proxy.get_httpx_client()
    for name, url in CHECK_URLS.items():
        try:
            resp = await client.get(url, timeout=15.0)
            ok = resp.status_code < 500
            results.append(CheckResult(name, ok, f"status={resp.status_code}"))
        except Exception as e:
            results.append(CheckResult(name, False, str(e)))

    # Polymarket 地区限制（与 CLOB 下单走同一出口）
    # 注意：代理偶发返回 HTML 而非 JSON，不能据此误判 geoblocked
    geoblock_ok = False
    geoblock_detail = "probe_failed"
    clob_ok = any(r.name == "clob" and r.ok for r in results)
    for attempt in range(3):
        try:
            resp = await client.get(
                GEOBLOCK_URL,
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
            if resp.status_code >= 500:
                geoblock_detail = f"status={resp.status_code}"
                await asyncio.sleep(1.0)
                continue
            text = resp.text.strip()
            if not text.startswith("{"):
                geoblock_detail = f"non_json status={resp.status_code}"
                await asyncio.sleep(1.0)
                continue
            data = resp.json()
            blocked = data.get("blocked") is True
            geoblock_detail = (
                f"blocked={blocked} country={data.get('country')} ip={data.get('ip')} "
                f"region={data.get('region', '')}"
            )
            geoblock_ok = not blocked
            break
        except Exception as e:
            geoblock_detail = str(e)
            await asyncio.sleep(1.0)
    # CLOB 可用但 geoblock 探测失败时，视为探测不确定，勿锁死下单
    if not geoblock_ok and clob_ok and (
        "non_json" in geoblock_detail or "Expecting value" in geoblock_detail
    ):
        geoblock_ok = True
        geoblock_detail = f"inconclusive_clob_ok ({geoblock_detail})"
    results.append(CheckResult("geoblock", geoblock_ok, geoblock_detail))

    # nba_api 为可选源（ESPN NBA 兜底），失败仅告警，不参与 live 暂停判定
    try:
        from src.sports.nba_api_provider import _fetch_scoreboard_sync

        with proxy.requests_env():
            games = await asyncio.to_thread(_fetch_scoreboard_sync)
        results.append(CheckResult("nba_api", True, f"games={len(games)}"))
    except Exception as e:
        results.append(CheckResult("nba_api", False, f"optional: {e}"))

    await proxy.aclose()
    return results


def critical_failures(results: list[CheckResult]) -> list[CheckResult]:
    """仅返回会导致无法交易的关键失败项。"""
    return [r for r in results if not r.ok and r.name in CRITICAL_CHECKS]


def optional_failures(results: list[CheckResult]) -> list[CheckResult]:
    """可选数据源失败（不影响下单开关）。"""
    return [r for r in results if not r.ok and r.name not in CRITICAL_CHECKS]


async def main_async(config_path: str) -> int:
    cfg = load_config(config_path)
    proxy = ProxyTransport(cfg.proxy)
    results = await run_health_checks(cfg, proxy)
    all_ok = True
    for r in results:
        status = "OK" if r.ok else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")
        if not r.ok:
            all_ok = False
    return 0 if all_ok else 1


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "config.local.yaml"
    code = asyncio.run(main_async(path))
    sys.exit(code)


if __name__ == "__main__":
    main()
