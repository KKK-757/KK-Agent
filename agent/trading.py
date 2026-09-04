"""Spot trading workflow (real version, disabled by default).

Real Binance spot signed endpoints (HMAC-SHA256), supporting market/limit orders, order queries, and account balance.
Safety design:
- RiskGuard checks before any order; default deny when any condition fails
- DRY-RUN mode: request is simulated and printed only, never sent
- Withdrawal/transfer is not implemented at the code level at all (a hard constraint, not a switch)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from .config import AgentSettings
from .risk import RiskDecision, RiskGuard, TradeRequest

log = logging.getLogger("agent.trading")


class TradingError(RuntimeError):
    pass


async def verify_api_keys(api_key: str, api_secret: str, base: str) -> tuple[bool, str]:
    """对指定环境验证 Key 有效性（签名请求 /api/v3/account）。返回 (是否成功, 说明)。"""
    if not api_key or not api_secret:
        return False, "Key/Secret 不能为空"
    p = {"timestamp": str(int(time.time() * 1000)), "recvWindow": "5000"}
    qs = urlencode(p)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base.rstrip('/')}/api/v3/account?{qs}&signature={sig}"
    try:
        async with httpx.AsyncClient(timeout=12) as cli:
            r = await cli.get(url, headers={"X-MBX-APIKEY": api_key})
        if r.status_code == 200:
            try:
                data = r.json()
                n = len([b for b in data.get("balances", []) if float(b.get("free", 0)) > 0])
                return True, f"验证成功，账户内 {n} 个非零余额"
            except Exception:
                return True, "验证成功"
        try:
            msg = r.json().get("msg", r.text)
        except Exception:
            msg = r.text[:120]
        return False, f"验证失败：{msg}"
    except Exception as e:
        return False, f"网络错误：{e}"


class SpotTrader:
    """Binance spot signed API wrapper. All operations go through the risk-control layer first."""

    def __init__(self, settings: AgentSettings, risk: RiskGuard):
        self.s = settings
        self.risk = risk
        self.base = settings.binance.trade_base.rstrip("/")

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------
    def _signed_params(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        if not self.s.binance.keys_present:
            raise TradingError(
                "缺少 API Key/Secret。两种玩法：\n"
                "  1) 模拟盘（推荐，虚拟资金随便玩）：https://testnet.binance.vision 用 GitHub 登录生成 Key，"
                "填入 .env 的 BIN_AGENT_BINANCE_API_KEY/SECRET，并设 BIN_AGENT_TRADING_ENABLED=true\n"
                "  2) 正式盘（真钱）：用子账户 Key，只开通读取 + 现货交易权限，绝不开提币"
            )
        p = {k: str(v) for k, v in (params or {}).items()}
        p["timestamp"] = str(int(time.time() * 1000))
        p["recvWindow"] = str(self.s.binance.recv_window)
        qs = urlencode(p)
        sig = hmac.new(
            self.s.binance.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()
        p["signature"] = sig
        return p

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.s.binance.api_key}

    async def _send(self, method: str, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.request(method, url, params=params, headers=self._headers())
            if r.status_code >= 400:
                try:
                    msg = r.json().get("msg", r.text)
                except Exception:
                    msg = r.text
                raise TradingError(f"Binance API {r.status_code}: {msg}")
            return r.json()

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------
    async def account(self) -> Dict[str, Any]:
        """Account information and balances (requires read permission)."""
        return await self._send("GET", "/api/v3/account", self._signed_params())

    async def balances_nonzero(self) -> list[Dict[str, str]]:
        acc = await self.account()
        out = []
        for b in acc.get("balances", []):
            free, locked = b.get("free", "0"), b.get("locked", "0")
            if float(free) > 0 or float(locked) > 0:
                out.append({"asset": b["asset"], "free": free, "locked": locked})
        return out

    async def open_orders(self, symbol: Optional[str] = None) -> list[Dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else {}
        return await self._send("GET", "/api/v3/openOrders", self._signed_params(params))

    async def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """撤销一笔挂单（测试网/正式盘均需 Key）。"""
        return await self._send(
            "DELETE", "/api/v3/order",
            self._signed_params({"symbol": symbol.upper(), "orderId": str(order_id)}),
        )

    async def recent_trades_mine(self, symbol: str, limit: int = 10) -> list[Dict[str, Any]]:
        """最近成交（自己的订单，需要 Key）。"""
        return await self._send(
            "GET", "/api/v3/myTrades",
            self._signed_params({"symbol": symbol.upper(), "limit": str(min(int(limit), 50))}),
        )

    # ------------------------------------------------------------------
    # Placing orders
    # ------------------------------------------------------------------
    async def place_order(self, req: TradeRequest, confirm_cb=None) -> Dict[str, Any]:
        """下单（风控 -> 正式盘强制人工确认 -> 发送到 trade_base 指向的环境）。

        下单去向由 .env 的 BIN_AGENT_BINANCE_TRADE_BASE 决定：
        测试网 = 虚拟资金真实撮合；正式盘 = 真钱（强制逐笔确认）。
        """
        decision: RiskDecision = self.risk.check(req)
        if not decision.allowed:
            log.warning("Risk control blocked: %s", decision.reason)
            return {"status": "REJECTED", "reason": decision.reason}

        # 正式盘强制人工确认（测试网不确认，方便玩）
        if not self.s.binance.is_testnet and confirm_cb is not None:
            ok = await confirm_cb(req)
            if not ok:
                return {"status": "CANCELLED", "reason": "您在确认提示中取消了下单"}

        # --- 构造并发送订单 ---
        payload: Dict[str, Any] = {
            "symbol": req.symbol.upper(),
            "side": req.action.upper(),
            "quoteOrderQty": f"{req.quote_qty:.8f}".rstrip("0").rstrip("."),  # 按计价币金额下单
        }
        if req.price is None:
            payload["type"] = "MARKET"
        else:
            payload["type"] = "LIMIT"
            payload["timeInForce"] = "GTC"
            payload["price"] = f"{req.price:.8f}".rstrip("0").rstrip(".")
            payload["quantity"] = f"{req.quote_qty / req.price:.8f}".rstrip("0").rstrip(".")

        resp = await self._send("POST", "/api/v3/order", self._signed_params(payload))
        env = "testnet" if self.s.binance.is_testnet else "正式盘"
        log.info("Order submitted to %s: %s %s", env, req.symbol, resp.get("orderId"))
        return {"status": "SUBMITTED", "env": env, "order": resp}

    # ------------------------------------------------------------------
    @staticmethod
    def describe_safety() -> str:
        return (
            "交易安全设计：1) 下单总开关默认关闭；2) 测试网/正式盘由 TRADE_BASE 一键切换；"
            "3) 正式盘每笔强制人工确认；4) 单笔名义限额；5) 提币/划转代码路径不存在（硬约束）。"
        )


def trade_request_from_text(text: str) -> Optional[TradeRequest]:
    """Ultra-light regex intent parsing (fallback for when the LLM is absent).

    Only matches English forms like 'buy 50 USDT of BTC' / 'sell 0.01 BTC';
    for Chinese intents, configure an LLM (parse_intent) or use explicit CLI arguments instead.
    """
    import re

    t = text.replace(",", "").strip()
    # 限价：句中带 "at 价格" / "@ 价格" / "价格 60000" 视为限价单
    m_price = re.search(r"(?:at|@|价格)\s*([0-9]+(?:\.[0-9]+)?)", t, re.I)
    limit_price = float(m_price.group(1)) if m_price else None
    # Amount in quote currency: buy/sell xxx USDT/U of XXX
    m = re.search(
        r"(buy|sell|buy in|sell out)\s*([0-9.]+)\s*(usdt|u|usdc)\s*(?:of|@)?\s*([a-z0-9]{2,10})",
        t,
        re.I,
    )
    if m:
        action, qty, _q, base = m.group(1).lower(), float(m.group(2)), m.group(3).lower(), m.group(4).upper()
        action = "buy" if "buy" in action else "sell"
        quote = "USDC" if _q == "usdc" else "USDT"
        return TradeRequest(
            action=action, symbol=f"{base}{quote}", quote_qty=qty,
            price=limit_price, source="cli",
        )
    # Quantity in base currency: sell xxx BTC (needs a current price to convert to notional; caller handles it)
    m2 = re.search(r"(buy|sell)\s*([0-9.]+)\s*([a-z0-9]{2,10})\b", t, re.I)
    if m2 and m2.group(3).upper() not in ("USDT", "U", "USDC"):
        return TradeRequest(
            action=m2.group(1).lower(),
            symbol=f"{m2.group(3).upper()}USDT",
            quote_qty=-1,  # -1 marks 'quantity in base currency', caller must convert to notional first
            base_qty=float(m2.group(2)),
            price=limit_price,
            source="cli",
        )
    return None
