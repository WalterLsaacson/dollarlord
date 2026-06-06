"""
真金小额往返测试：市价买入 → 市价卖出（验证 PK / FUNDER / CLOB 全流程）。

用法:
    .venv/bin/python scripts/live_roundtrip_test.py --usd 0.1
    .venv/bin/python scripts/live_roundtrip_test.py --usd 1 --no-proxy   # 1080 未开时直连
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 保证可 import src
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.net.proxy import ProxyTransport


async def pick_liquid_token(cfg, proxy: ProxyTransport) -> tuple[str, str, float, float]:
    """从 Gamma 挑一个有卖单深度的活跃 token。"""
    client = await proxy.get_httpx_client()
    resp = await client.get(
        f"{cfg.gamma_base_url}/markets",
        params={"limit": 50, "active": "true", "closed": "false"},
        timeout=30.0,
    )
    resp.raise_for_status()
    markets = resp.json()
    if not isinstance(markets, list):
        markets = markets.get("data", markets) if isinstance(markets, dict) else []

    best: tuple[str, str, float, float] | None = None
    for m in markets:
        if m.get("closed"):
            continue
        tokens = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                continue
        if not tokens or not isinstance(tokens, list):
            continue
        token_id = str(tokens[0])
        book_resp = await client.get(
            f"{cfg.clob_host}/book",
            params={"token_id": token_id},
            timeout=15.0,
        )
        if book_resp.status_code != 200:
            continue
        book = book_resp.json()
        asks = book.get("asks") or []
        if not asks:
            continue
        # asks: price, size
        ask0 = asks[0]
        price = float(ask0.get("price") or ask0[0] if isinstance(ask0, dict) else ask0[0])
        size = float(ask0.get("size") or ask0[1] if isinstance(ask0, dict) else ask0[1])
        if price <= 0 or price > 0.99 or size <= 0:
            continue
        bids = book.get("bids") or []
        if not bids:
            continue
        bid0 = bids[-1] if bids else None
        bid_price = float(bid0.get("price") or 0) if bid0 else 0
        if bid_price <= 0:
            continue
        depth_usd = price * size
        question = (m.get("question") or m.get("title") or "")[:60]
        cand = (token_id, question, price, depth_usd)
        if best is None or depth_usd > best[3]:
            best = cand

    if not best:
        raise RuntimeError("未找到合适盘口（需有卖单且价格 0.01~0.98）")
    return best


def build_clob_client(cfg, proxy: ProxyTransport):
    from py_clob_client_v2 import ApiCreds, ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    pk = os.environ.get("PK")
    if not pk:
        raise RuntimeError("缺少 PK，请写入 polymarket-arb.env")

    host = cfg.clob_host
    chain_id = 137
    funder = os.environ.get("FUNDER") or None
    if funder:
        from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

        sig = int(SignatureTypeV2.POLY_1271)
    else:
        sig = None

    with proxy.requests_env():
        bootstrap = ClobClient(host=host, chain_id=chain_id, key=pk)
        api_key = os.environ.get("CLOB_API_KEY")
        api_secret = os.environ.get("CLOB_SECRET")
        api_pass = os.environ.get("CLOB_PASS_PHRASE")
        if api_key and api_secret and api_pass:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_pass,
            )
            probe = ClobClient(
                host=host,
                chain_id=chain_id,
                key=pk,
                creds=creds,
                funder=funder,
                signature_type=sig,
            )
            try:
                probe.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
                )
            except Exception:
                creds = bootstrap.create_or_derive_api_key()
        else:
            creds = bootstrap.create_or_derive_api_key()

    return ClobClient(
        host=host,
        chain_id=chain_id,
        key=pk,
        creds=creds,
        funder=funder,
        signature_type=sig,
    )


def _filled_shares(buy_resp: dict) -> float:
    """从成交回报读取买到的份额（BUY 时 takingAmount 为 shares）。"""
    for key in ("takingAmount", "size", "filled_size"):
        v = buy_resp.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    # 兜底：用 USDC / 成交价
    spent = buy_resp.get("amount") or buy_resp.get("cost")
    price = buy_resp.get("price") or buy_resp.get("avg_price")
    if spent and price:
        try:
            return float(spent) / float(price)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return 0.0


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="真金买卖往返测试")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--usd", type=float, default=1.0, help="买入 USDC 名义金额（CLOB 最低 $1）")
    parser.add_argument("--no-proxy", action="store_true", help="直连（1080 未开时用）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.no_proxy:
        cfg.proxy.enabled = False
        print("[提示] 已关闭代理，直连 CLOB/Gamma")

    proxy = ProxyTransport(cfg.proxy)
    try:
        token_id, question, ask, depth = await pick_liquid_token(cfg, proxy)
        print(f"选中盘口: {question}")
        print(f"token_id={token_id} best_ask={ask} depth_usd≈{depth:.2f}")

        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions

        client = build_clob_client(cfg, proxy)
        buy_usd = max(args.usd, 1.0)
        if args.usd < 1:
            print("[提示] CLOB 市价买单最低 $1，已自动调整为 1.0")

        print(f"\n=== 1/2 市价买入 FOK ${buy_usd} ===")
        with proxy.requests_env():
            buy_resp = client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=token_id,
                    amount=buy_usd,
                    side=Side.BUY,
                    order_type=OrderType.FOK,
                ),
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.FOK,
            )
        print("买入回报:", json.dumps(buy_resp, default=str)[:800])

        if isinstance(buy_resp, dict):
            status = str(buy_resp.get("status", "")).lower()
            if "matched" not in status and status != "matched":
                print("买入未成交，终止（不执行卖出）")
                return 1

        shares = _filled_shares(buy_resp if isinstance(buy_resp, dict) else {})
        if shares <= 0:
            shares = buy_usd / ask
        if shares <= 0:
            print("无法估算卖出份额")
            return 1

        # 卖出前同步 conditional token 余额/授权（否则可能报 balance: 0）
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        cond_params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
        )
        with proxy.requests_env():
            try:
                client.update_balance_allowance(cond_params)
            except Exception as e:
                print(f"[警告] conditional 授权同步失败: {e}")

        print(f"\n=== 2/2 市价卖出 FOK {shares} 份 ===")
        with proxy.requests_env():
            sell_resp = client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=token_id,
                    amount=shares,
                    side=Side.SELL,
                    order_type=OrderType.FOK,
                ),
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.FOK,
            )
        print("卖出回报:", json.dumps(sell_resp, default=str)[:800])

        if isinstance(sell_resp, dict):
            st = str(sell_resp.get("status", "")).lower()
            if "matched" in st or st == "matched":
                print("\n往返测试完成：买 + 卖均已成交")
                return 0
        print("\n卖出可能未完全成交，请上 Polymarket 查看持仓")
        return 1
    finally:
        await proxy.aclose()


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
