"""分级 FOK 下单（paper / live）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from src.config import AppConfig
from src.engine.risk import RiskManager
from src.logging_setup import log_event
from src.net.proxy import ProxyTransport
from src.pm.clob_ws import OrderbookSnapshot
from src.store.sqlite import Store

logger = logging.getLogger("arb.ladder")


def _order_step_log(ctx: dict[str, Any], **fields: Any) -> None:
    """写 STRATEGY_ORDER_STEP 日志；先合并 ctx 再覆盖 fields，避免 **ctx 与显式参数重名导致 TypeError。"""
    log_event(logger, "STRATEGY_ORDER_STEP", **{**ctx, **fields})


@dataclass
class LadderResult:
    """下单结果。"""

    success: bool
    filled_usd: float
    price: float
    status: str
    detail: str


class LadderExecutor:
    """按流动性阶梯尝试买入。"""

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        risk: RiskManager,
        proxy: ProxyTransport,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.risk = risk
        self.proxy = proxy
        self._live_client = None

    # CLOB 市价买单最低名义金额（美元）
    _CLOB_MIN_BUY_USD = 1.0

    def _signature_type(self) -> int | None:
        """有 FUNDER（Deposit/Proxy Wallet）时用 POLY_1271=3（V2 充值钱包流程）。"""
        if os.environ.get("FUNDER"):
            from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

            return int(SignatureTypeV2.POLY_1271)
        return None

    def _creds_invalid(self, exc: BaseException) -> bool:
        """判断是否为 CLOB API 凭证失效。"""
        msg = str(exc).lower()
        return "401" in msg or "unauthorized" in msg or "invalid api key" in msg

    def _resolve_api_creds(self, bootstrap: Any, pk: str, funder: str | None) -> Any:
        """优先用环境变量三件套；校验失败则 derive。"""
        from py_clob_client_v2 import ApiCreds, ClobClient
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        api_key = os.environ.get("CLOB_API_KEY")
        api_secret = os.environ.get("CLOB_SECRET")
        api_pass = os.environ.get("CLOB_PASS_PHRASE")
        sig = self._signature_type()
        host = self.cfg.clob_host
        chain_id = 137

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
                with self.proxy.requests_env():
                    probe.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
                    )
                logger.info("使用环境变量 CLOB API 凭证")
                return creds
            except Exception as e:
                if self._creds_invalid(e):
                    logger.warning("环境变量 CLOB 凭证无效，改用 derive: %s", e)
                else:
                    logger.warning("CLOB 凭证校验失败，改用 derive: %s", e)

        with self.proxy.requests_env():
            creds = bootstrap.create_or_derive_api_key()
        logger.info("已 derive CLOB API 凭证")
        return creds

    def _init_live_client(self) -> bool:
        """延迟初始化 CLOB 客户端（下单走与行情相同的代理出口）。"""
        if self._live_client is not None:
            return True
        try:
            from py_clob_client_v2 import ClobClient
        except ImportError:
            logger.error("未安装 py-clob-client-v2，请: pip install 'polymarket-settlement-arb[live]'")
            return False

        pk = os.environ.get("PK")
        if not pk:
            logger.error("live 模式需要环境变量 PK（写入 polymarket-arb.env）")
            return False

        host = self.cfg.clob_host
        chain_id = 137
        funder = os.environ.get("FUNDER") or None
        sig = self._signature_type()

        with self.proxy.requests_env():
            bootstrap = ClobClient(host=host, chain_id=chain_id, key=pk)
            creds = self._resolve_api_creds(bootstrap, pk, funder)

        self._live_client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=pk,
            creds=creds,
            funder=funder,
            signature_type=sig,
        )
        return True

    def _get_available_usdc(self) -> float | None:
        """查询 CLOB 侧可用 USDC（6 位小数）；失败返回 None 不阻断下单。"""
        if not self._init_live_client():
            return None
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            with self.proxy.requests_env():
                bal = self._live_client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
                )
            raw = int(bal.get("balance", 0) or 0)
            return raw / 1_000_000.0
        except Exception as e:
            logger.warning("查询 USDC 余额失败: %s", e)
            return None

    def _parse_live_order_response(
        self,
        resp: Any,
        amount_usd: float,
        price: float,
    ) -> LadderResult:
        """解析 CLOB 下单回报：matched 与 delayed（异步成交）均视为成功。"""
        if not isinstance(resp, dict):
            status = str(resp).lower()
            if "matched" in status:
                return LadderResult(True, amount_usd, price, "matched", str(resp))
            return LadderResult(False, 0, price, status, str(resp))

        status = str(resp.get("status", "")).lower()
        # delayed = 已提交，链上/撮合异步完成（Slovenia 三笔即此情况）
        if status in ("matched", "delayed"):
            filled = amount_usd
            making = resp.get("makingAmount")
            if making not in (None, ""):
                try:
                    filled = float(making)
                    # 大额整数为链上 6 位精度 USDC
                    if filled >= 1_000_000:
                        filled /= 1_000_000.0
                except (TypeError, ValueError):
                    filled = amount_usd
            return LadderResult(True, filled, price, status, str(resp))

        return LadderResult(False, 0, price, status or "unknown", str(resp))

    def _is_balance_error(self, detail: str) -> bool:
        """余额/授权不足（常见于结算占用 USDC）。"""
        d = detail.lower()
        return "not enough balance" in d or "insufficient balance" in d

    async def execute_buy(
        self,
        market_id: str,
        token_id: str,
        book: OrderbookSnapshot,
        strategy_ctx: dict[str, Any] | None = None,
    ) -> LadderResult:
        """分级尝试买入胜方 token。"""
        ctx = dict(strategy_ctx or {})
        ctx.setdefault("market_id", market_id)
        ctx.setdefault("token_id", token_id)

        ok, reason = self.risk.can_trade(market_id)
        if not ok:
            _order_step_log(ctx, step="risk_block", reason=reason)
            self.store.record_signal_event(
                market_id=market_id,
                event_type="risk_block",
                reason=reason,
                detail=reason,
                sport=str(ctx.get("sport", "")),
                team_a=str(ctx.get("team_a", "")),
                team_b=str(ctx.get("team_b", "")),
            )
            return LadderResult(False, 0, 0, "skipped", reason)

        if book.best_ask is None:
            _order_step_log(ctx, step="abort", reason="no_ask")
            return LadderResult(False, 0, 0, "ILLIQUID", "无卖单")

        if book.best_ask < 0.01:
            _order_step_log(ctx, step="abort", reason="ask_below_clob_min", best_ask=book.best_ask)
            return LadderResult(False, 0, book.best_ask, "invalid_price", "ask < 0.01")

        if book.best_ask > self.cfg.entry_max_price:
            _order_step_log(
                ctx,
                step="abort",
                reason="ask_above_max",
                best_ask=book.best_ask,
                entry_max_price=self.cfg.entry_max_price,
            )
            return LadderResult(
                False,
                0,
                book.best_ask,
                "no_edge",
                f"ask={book.best_ask} > max={self.cfg.entry_max_price}",
            )

        if book.best_ask < self.cfg.early_entry_price:
            _order_step_log(
                ctx,
                step="abort",
                reason="ask_below_min",
                best_ask=book.best_ask,
                early_entry_price=self.cfg.early_entry_price,
            )
            return LadderResult(
                False,
                0,
                book.best_ask,
                "no_edge",
                f"ask={book.best_ask} < min={self.cfg.early_entry_price}",
            )

        available = book.available_notional(self.cfg.entry_max_price)
        ctx["depth_usd"] = round(available, 4)
        if available < self.cfg.min_ladder_step_usd:
            _order_step_log(
                ctx,
                step="abort",
                reason="insufficient_depth",
                depth_usd=available,
                min_ladder_step_usd=self.cfg.min_ladder_step_usd,
            )
            return LadderResult(False, 0, book.best_ask, "ILLIQUID", f"depth={available:.2f}")

        ladder = self.cfg.get_order_ladder()
        usdc_available: float | None = None
        if self.cfg.mode == "live":
            usdc_available = self._get_available_usdc()
            if usdc_available is not None:
                ctx["usdc_available"] = round(usdc_available, 4)
                min_need = max(self.cfg.min_ladder_step_usd, self._CLOB_MIN_BUY_USD)
                if usdc_available < min_need:
                    _order_step_log(
                        ctx,
                        step="abort",
                        reason="insufficient_usdc",
                        usdc_available=usdc_available,
                        min_need=min_need,
                    )
                    return LadderResult(
                        False,
                        0,
                        book.best_ask,
                        "insufficient_balance",
                        f"usdc={usdc_available:.2f}",
                    )

        ctx["ladder_usd"] = ladder
        for amount_usd in ladder:
            amount_usd = min(amount_usd, self.cfg.max_round_notional_usd, available)
            if usdc_available is not None:
                amount_usd = min(amount_usd, usdc_available)
            min_step = max(self.cfg.min_ladder_step_usd, self._CLOB_MIN_BUY_USD)
            if amount_usd < min_step:
                continue

            _order_step_log(ctx, step="attempt", amount_usd=amount_usd, best_ask=book.best_ask)

            if self.cfg.mode == "paper":
                result = self._paper_fill(market_id, token_id, book, amount_usd)
            else:
                result = await self._live_fill(market_id, token_id, book, amount_usd)

            if result.success:
                self.risk.set_geoblocked(False)
                self.risk.record_success(market_id)
                self.store.record_trade(
                    market_id,
                    self.cfg.mode,
                    result.filled_usd,
                    result.price,
                    result.status,
                    result.detail,
                )
                _order_step_log(
                    ctx,
                    step="filled",
                    amount_usd=amount_usd,
                    filled_usd=result.filled_usd,
                    price=result.price,
                    status=result.status,
                )
                return result

            _order_step_log(
                ctx,
                step="ladder_miss",
                amount_usd=amount_usd,
                status=result.status,
                detail=result.detail,
            )
            # 余额不足：结算占用资金，不计连续失败，直接结束本轮
            if result.status == "insufficient_balance" or self._is_balance_error(result.detail):
                _order_step_log(ctx, step="abort", reason="insufficient_usdc_on_submit", detail=result.detail[:200])
                return LadderResult(
                    False,
                    0,
                    book.best_ask,
                    "insufficient_balance",
                    result.detail,
                )
            # geoblock / 无效凭证 / 无效价格 / delayed 已成功则不会走到这里
            if result.status in ("geoblocked", "invalid_price", "auth_error"):
                return result

        self.risk.record_failure(count=True)
        _order_step_log(ctx, step="exhausted", reason="all_ladder_failed")
        return LadderResult(False, 0, book.best_ask, "ladder_exhausted", "所有阶梯均未成交")

    def _paper_fill(
        self,
        market_id: str,
        token_id: str,
        book: OrderbookSnapshot,
        amount_usd: float,
    ) -> LadderResult:
        """模拟 FOK：检查深度是否足够。"""
        filled = 0.0
        cost = 0.0
        for price, size in book.asks:
            if price > self.cfg.entry_max_price:
                break
            take_usd = min(amount_usd - cost, price * size)
            if take_usd <= 0:
                break
            shares = take_usd / price
            cost += shares * price
            filled += shares * price
            if cost >= amount_usd - 0.01:
                break

        if filled >= amount_usd * 0.99:
            avg_price = book.best_ask or self.cfg.entry_max_price
            return LadderResult(True, filled, avg_price, "paper_matched", "simulated FOK")
        return LadderResult(False, 0, book.best_ask or 0, "FOK_NOT_FILLED", "深度不足")

    async def _live_fill(
        self,
        market_id: str,
        token_id: str,
        book: OrderbookSnapshot,
        amount_usd: float,
    ) -> LadderResult:
        """真金 FOK 市价买（HTTP 走代理环境变量）。"""
        if not self._init_live_client():
            return LadderResult(False, 0, 0, "no_client", "CLOB 客户端未就绪")

        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
            from py_clob_client_v2.clob_types import PartialCreateOrderOptions

            price = min(self.cfg.entry_max_price, (book.best_ask or 0.99) + 0.01)
            price = round(price, 2)

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
                side=Side.BUY,
                order_type=OrderType.FOK,
            )
            with self.proxy.requests_env():
                resp = self._live_client.create_and_post_market_order(
                    order_args=order_args,
                    options=PartialCreateOrderOptions(tick_size="0.01"),
                    order_type=OrderType.FOK,
                )
            return self._parse_live_order_response(resp, amount_usd, price)
        except Exception as e:
            err = str(e)
            if self._is_balance_error(err):
                return LadderResult(False, 0, book.best_ask or 0, "insufficient_balance", err)
            if self._creds_invalid(e):
                self._live_client = None
                return LadderResult(False, 0, 0, "auth_error", err)
            if "geoblock" in err.lower() or "restricted in your region" in err.lower():
                self.risk.set_geoblocked(True, err[:200])
                return LadderResult(False, 0, 0, "geoblocked", err)
            if "invalid price" in err.lower():
                return LadderResult(False, 0, book.best_ask or 0, "invalid_price", err)
            if "FOK" in err.upper() or "NOT_FILLED" in err.upper():
                return LadderResult(False, 0, book.best_ask or 0, "FOK_NOT_FILLED", err)
            logger.exception("live 下单异常")
            return LadderResult(False, 0, 0, "error", err)
