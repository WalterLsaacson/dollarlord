"""Polymarket 持仓结算（redeem）：通过 Gnosis Safe 代理钱包链上赎回 USDC。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from eth_abi import encode as abi_encode
from eth_keys import keys as eth_keys
from web3 import Web3

from src.config import AppConfig
from src.logging_setup import log_event
from src.net.proxy import ProxyTransport
from src.store.sqlite import Store

logger = logging.getLogger("arb.redeem")

# Polygon 主网合约
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
ZERO_ADDRESS = "0x" + "00" * 20
DATA_API = "https://data-api.polymarket.com/positions"

CTF_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]


@dataclass
class PositionView:
    """Data API 持仓 + 是否满足自动结算条件。"""

    condition_id: str
    title: str
    asset: str
    opposite_asset: str
    outcome_index: int
    size: float
    cur_price: float
    redeemable: bool
    negative_risk: bool
    slug: str = ""

    def is_winner_at(self, threshold: float) -> bool:
        """胜方 token 价格是否达到自动结算阈值（如 0.998 ≈ 99.8%）。"""
        return self.cur_price >= threshold


@dataclass
class RedeemOutcome:
    """单次结算结果。"""

    ok: bool
    condition_id: str
    title: str
    tx_hash: str = ""
    detail: str = ""
    usdc_gained: float = 0.0


class RedeemService:
    """查询可结算持仓并经由 Safe execTransaction 执行 redeem。"""

    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        proxy: ProxyTransport,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.proxy = proxy
        self._w3: Web3 | None = None

    def _is_winner(self, pos: PositionView) -> bool:
        """是否达到配置的自动结算价格阈值。"""
        return pos.is_winner_at(self.cfg.redeem_price_threshold)

    def enabled(self) -> bool:
        """live 模式且配置了 FUNDER + PK 时可结算。"""
        return (
            self.cfg.mode == "live"
            and bool(os.environ.get("FUNDER"))
            and bool(os.environ.get("PK"))
        )

    def _proxy_wallet(self) -> str:
        return str(os.environ.get("FUNDER", "")).strip()

    def _private_key(self) -> str:
        return str(os.environ.get("PK", "")).strip()

    def _web3(self) -> Web3:
        if self._w3 is None:
            self._w3 = Web3(Web3.HTTPProvider(self.cfg.polygon_rpc_url))
        return self._w3

    async def fetch_positions(self) -> list[PositionView]:
        """从 Polymarket Data API 拉取当前代理钱包持仓。"""
        wallet = self._proxy_wallet()
        if not wallet:
            return []
        client = await self.proxy.get_httpx_client()
        resp = await client.get(DATA_API, params={"user": wallet.lower()})
        resp.raise_for_status()
        raw = resp.json()
        out: list[PositionView] = []
        for p in raw:
            try:
                out.append(
                    PositionView(
                        condition_id=str(p.get("conditionId") or ""),
                        title=str(p.get("title") or p.get("slug") or ""),
                        asset=str(p.get("asset") or ""),
                        opposite_asset=str(p.get("oppositeAsset") or ""),
                        outcome_index=int(p.get("outcomeIndex") or 0),
                        size=float(p.get("size") or 0),
                        cur_price=float(p.get("curPrice") or 0),
                        redeemable=bool(p.get("redeemable")),
                        negative_risk=bool(p.get("negativeRisk")),
                        slug=str(p.get("slug") or ""),
                    )
                )
            except (TypeError, ValueError):
                continue
        return out

    def _ctf_balance(self, w3: Web3, proxy: str, token_id: str) -> int:
        """链上 ERC1155 份额（原始整数）。"""
        if not token_id:
            return 0
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_BALANCE_ABI,
        )
        return int(ctf.functions.balanceOf(Web3.to_checksum_address(proxy), int(token_id)).call())

    def _is_resolved_on_chain(self, w3: Web3, condition_id: str) -> bool:
        """CTF payoutDenominator > 0 表示已 resolve。"""
        selector = w3.keccak(text="payoutDenominator(bytes32)")[:4]
        call_data = selector + abi_encode(
            ["bytes32"],
            [bytes.fromhex(condition_id.replace("0x", ""))],
        )
        result = w3.eth.call({"to": Web3.to_checksum_address(CTF_ADDRESS), "data": call_data})
        return int(result.hex(), 16) > 0

    def _build_standard_redeem_calldata(self, w3: Web3, condition_id: str) -> bytes:
        """标准市场：CTF.redeemPositions(USDC, 0, conditionId, [1,2])。"""
        redeem_selector = w3.keccak(
            text="redeemPositions(address,bytes32,bytes32,uint256[])"
        )[:4]
        return redeem_selector + abi_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [
                Web3.to_checksum_address(USDC_ADDRESS),
                b"\x00" * 32,
                bytes.fromhex(condition_id.replace("0x", "")),
                [1, 2],
            ],
        )

    def _build_neg_risk_redeem_calldata(
        self,
        w3: Web3,
        condition_id: str,
        proxy: str,
        pos: PositionView,
    ) -> bytes | None:
        """Neg-risk 市场：Adapter.redeemPositions(conditionId, [yesAmt, noAmt])。"""
        if pos.outcome_index == 0:
            yes_token, no_token = pos.asset, pos.opposite_asset
        else:
            yes_token, no_token = pos.opposite_asset, pos.asset
        yes_bal = self._ctf_balance(w3, proxy, yes_token)
        no_bal = self._ctf_balance(w3, proxy, no_token)
        if yes_bal <= 0 and no_bal <= 0:
            return None
        selector = w3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
        return selector + abi_encode(
            ["bytes32", "uint256[]"],
            [
                bytes.fromhex(condition_id.replace("0x", "")),
                [yes_bal, no_bal],
            ],
        )

    def _exec_via_safe(
        self,
        w3: Web3,
        proxy: str,
        to_address: str,
        calldata: bytes,
    ) -> str:
        """EOA 签名后通过 Safe.execTransaction 执行 calldata。"""
        pk_hex = self._private_key().replace("0x", "")
        wallet = w3.eth.account.from_key(self._private_key())
        pk = eth_keys.PrivateKey(bytes.fromhex(pk_hex))

        proxy_cs = Web3.to_checksum_address(proxy)
        target_cs = Web3.to_checksum_address(to_address)

        nonce_selector = w3.keccak(text="nonce()")[:4]
        safe_nonce = int(w3.eth.call({"to": proxy_cs, "data": nonce_selector}).hex(), 16)

        get_hash_selector = w3.keccak(
            text="getTransactionHash(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,uint256)"
        )[:4]
        get_hash_data = get_hash_selector + abi_encode(
            [
                "address",
                "uint256",
                "bytes",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "uint256",
            ],
            [target_cs, 0, calldata, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, safe_nonce],
        )
        safe_tx_hash = w3.eth.call({"to": proxy_cs, "data": get_hash_data})
        sig = pk.sign_msg_hash(safe_tx_hash)
        signature = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v + 27])

        exec_selector = w3.keccak(
            text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
        )[:4]
        exec_data = exec_selector + abi_encode(
            [
                "address",
                "uint256",
                "bytes",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "bytes",
            ],
            [target_cs, 0, calldata, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, signature],
        )

        gas_estimate = w3.eth.estimate_gas(
            {"from": wallet.address, "to": proxy_cs, "data": exec_data}
        )
        tx = w3.eth.account.sign_transaction(
            {
                "to": proxy_cs,
                "data": exec_data,
                "gas": gas_estimate + 50_000,
                "gasPrice": w3.eth.gas_price,
                "nonce": w3.eth.get_transaction_count(wallet.address),
                "chainId": 137,
            },
            self._private_key(),
        )
        tx_hash = w3.eth.send_raw_transaction(tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"链上 revert, tx={tx_hash.hex()}")
        return tx_hash.hex()

    def _usdc_balance(self, w3: Web3, proxy: str) -> float:
        selector = w3.keccak(text="balanceOf(address)")[:4]
        data = selector + abi_encode(["address"], [Web3.to_checksum_address(proxy)])
        raw = int(w3.eth.call({"to": Web3.to_checksum_address(USDC_ADDRESS), "data": data}).hex(), 16)
        return raw / 1_000_000.0

    def redeem_condition(
        self,
        condition_id: str,
        *,
        pos: PositionView | None = None,
        trigger: str = "manual",
        winner_only: bool = False,
    ) -> RedeemOutcome:
        """结算单个 condition（链上 redeem）。"""
        title = pos.title if pos else condition_id[:16]
        if not self.enabled():
            return RedeemOutcome(False, condition_id, title, detail="live 模式未配置 FUNDER/PK")

        if self.store.is_condition_redeemed(condition_id):
            return RedeemOutcome(False, condition_id, title, detail="该场次已结算过")

        if pos and winner_only and not self._is_winner(pos):
            return RedeemOutcome(False, condition_id, title, detail="非胜方持仓，跳过自动结算")

        w3 = self._web3()
        if not w3.is_connected():
            return RedeemOutcome(False, condition_id, title, detail=f"RPC 不可用: {self.cfg.polygon_rpc_url}")

        proxy = self._proxy_wallet()
        if not self._is_resolved_on_chain(w3, condition_id):
            return RedeemOutcome(False, condition_id, title, detail="链上尚未 resolve")

        # 仍有份额才结算
        if pos:
            bal = self._ctf_balance(w3, proxy, pos.asset)
            if bal <= 0:
                return RedeemOutcome(False, condition_id, title, detail="链上份额为 0")

        neg_risk = bool(pos and pos.negative_risk)
        if neg_risk:
            if not pos:
                return RedeemOutcome(False, condition_id, title, detail="neg_risk 需完整持仓信息")
            calldata = self._build_neg_risk_redeem_calldata(w3, condition_id, proxy, pos)
            target = NEG_RISK_ADAPTER
        else:
            calldata = self._build_standard_redeem_calldata(w3, condition_id)
            target = CTF_ADDRESS

        if not calldata:
            return RedeemOutcome(False, condition_id, title, detail="无可结算份额")

        usdc_before = self._usdc_balance(w3, proxy)
        try:
            tx_hash = self._exec_via_safe(w3, proxy, target, calldata)
        except Exception as e:
            log_event(
                logger,
                "REDEEM_FAIL",
                condition_id=condition_id,
                title=title,
                trigger=trigger,
                error=str(e)[:300],
            )
            self.store.record_redemption(
                condition_id=condition_id,
                title=title,
                size=pos.size if pos else 0,
                cur_price=pos.cur_price if pos else 0,
                tx_hash="",
                status="failed",
                detail=str(e)[:500],
                trigger=trigger,
            )
            return RedeemOutcome(False, condition_id, title, detail=str(e)[:300])

        usdc_after = self._usdc_balance(w3, proxy)
        gained = max(0.0, usdc_after - usdc_before)
        self.store.record_redemption(
            condition_id=condition_id,
            title=title,
            size=pos.size if pos else 0,
            cur_price=pos.cur_price if pos else 0,
            tx_hash=tx_hash,
            status="success",
            detail=f"usdc_gained={gained:.4f}",
            trigger=trigger,
            usdc_gained=gained,
        )
        log_event(
            logger,
            "REDEEM_OK",
            condition_id=condition_id,
            title=title,
            trigger=trigger,
            tx_hash=tx_hash,
            usdc_gained=gained,
        )
        return RedeemOutcome(True, condition_id, title, tx_hash=tx_hash, usdc_gained=gained)

    async def list_settlement_candidates(
        self,
        *,
        winner_only: bool = False,
    ) -> list[dict[str, Any]]:
        """返回可展示/可结算的持仓列表。"""
        positions = await self.fetch_positions()
        by_cond: dict[str, PositionView] = {}
        for p in positions:
            if not p.condition_id or p.size <= 0:
                continue
            if p.condition_id not in by_cond:
                by_cond[p.condition_id] = p

        items: list[dict[str, Any]] = []
        for p in by_cond.values():
            if winner_only and not self._is_winner(p):
                continue
            if not p.redeemable and not self._is_winner(p):
                continue
            items.append(
                {
                    "condition_id": p.condition_id,
                    "title": p.title,
                    "size": p.size,
                    "cur_price": p.cur_price,
                    "cur_price_pct": round(p.cur_price * 100, 1),
                    "redeemable": p.redeemable,
                    "is_winner": self._is_winner(p),
                    "negative_risk": p.negative_risk,
                    "already_redeemed": self.store.is_condition_redeemed(p.condition_id),
                    "slug": p.slug,
                }
            )
        items.sort(key=lambda x: (-float(x["cur_price"]), x["title"]))
        return items

    async def auto_redeem_winners(self) -> list[RedeemOutcome]:
        """自动结算：redeemable 且 curPrice≥阈值（≈100%）的胜方持仓。"""
        if not self.enabled() or not self.cfg.auto_redeem_enabled:
            return []

        positions = await self.fetch_positions()
        by_cond: dict[str, PositionView] = {}
        for p in positions:
            if p.condition_id:
                by_cond[p.condition_id] = p

        threshold = self.cfg.redeem_price_threshold
        results: list[RedeemOutcome] = []
        seen: set[str] = set()

        for p in by_cond.values():
            if p.condition_id in seen:
                continue
            seen.add(p.condition_id)
            if not p.redeemable:
                continue
            if p.cur_price < threshold:
                continue
            if self.store.is_condition_redeemed(p.condition_id):
                continue

            log_event(
                logger,
                "REDEEM_AUTO",
                condition_id=p.condition_id,
                title=p.title,
                cur_price=p.cur_price,
                size=p.size,
            )
            results.append(
                self.redeem_condition(
                    p.condition_id,
                    pos=p,
                    trigger="auto",
                    winner_only=True,
                )
            )
        return results

    async def redeem_all_manual(self, *, winners_only: bool = False) -> list[RedeemOutcome]:
        """手动结算： redeemable 持仓（可选仅胜方）。"""
        positions = await self.fetch_positions()
        by_cond: dict[str, PositionView] = {}
        for p in positions:
            if p.condition_id and p.redeemable:
                by_cond[p.condition_id] = p

        results: list[RedeemOutcome] = []
        for p in by_cond.values():
            if winners_only and not self._is_winner(p):
                continue
            if self.store.is_condition_redeemed(p.condition_id):
                continue
            results.append(
                self.redeem_condition(
                    p.condition_id,
                    pos=p,
                    trigger="manual",
                    winner_only=False,
                )
            )
        return results
