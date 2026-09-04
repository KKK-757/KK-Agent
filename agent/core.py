"""Agent main brain: tool orchestration and conversation scheduling.

Design: LLM is the "brain" and tools are the "hands". In mock mode (no Key) the brain degenerates into a rule engine,
but all tools remain fully usable — this is the key to the demo not being downgraded.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import AsyncIterator, Dict, List, Optional

from .config import AgentSettings, load_settings
from .llm import LLMClient, LLMError, parse_intent
from .market import MarketData, MarketError
from .mcp_support import full_guide as mcp_full_guide
from .payments_onchain import (
    OnchainCapability,
    PaymentCapability,
    capability_matrix,
    onchain_status,
    payment_status,
)
from .risk import RiskGuard, TradeRequest
from .signals import analyze
from .trading import SpotTrader, trade_request_from_text

log = logging.getLogger("agent.core")


class BinanceAgent:
    """The unified entry point for all capabilities: both the CLI and Web instantiate this."""

    def __init__(self, settings: Optional[AgentSettings] = None):
        self.s = settings or load_settings()
        self.llm = LLMClient(self.s)
        self.market = MarketData(self.s)
        self.risk = RiskGuard(self.s)
        self.trader = SpotTrader(self.s, self.risk)
        self.pay = PaymentCapability()
        self.onchain = OnchainCapability()
        self.history: List[Dict[str, str]] = []   # Conversation memory (last N turns)

    async def close(self) -> None:
        await self.market.close()

    # ==================================================================
    # Tool capabilities (each is async, can be called directly or invoked by the LLM)
    # ==================================================================
    async def tool_price(self, symbol: str) -> str:
        try:
            t = await self.market.ticker_price(symbol)
            t24 = await self.market.ticker_24h(symbol)
            pct = float(t24.get("priceChangePercent", 0))
            return (
                f"{t['symbol']} 最新价：{t['price']}\n"
                f"24h 涨跌：{pct:+.2f}% | 最高 {t24.get('highPrice')} / 最低 {t24.get('lowPrice')} | "
                f"24h 成交额 {float(t24.get('quoteVolume', 0)):,.0f} USDT"
            )
        except MarketError as e:
            return f"行情数据不可用：{e}"

    async def tool_signal(self, symbol: str, interval: Optional[str] = None) -> str:
        try:
            limit = int(self.s.market.get("kline_limit", 300))
            kl = await self.market.klines(symbol, interval or self.s.market.get("default_interval", "1h"), limit)
            rep = analyze(symbol, MarketData.klines_to_closes(kl), interval or "1h", self.s.signals)
            lines = [rep.summary, ""]
            for k, v in rep.details.items():
                lines.append(f"  [{k}] {v}")
            lines += ["", f"结论：{rep.verdict()}（得分 {rep.score:+d}/4）", RISK_NOTE]
            return "\n".join(lines)
        except (MarketError, ValueError) as e:
            return f"信号分析失败：{e}"

    async def tool_movers(self) -> str:
        try:
            top_n = int(self.s.market.get("top_n", 10))
            data = await self.market.top_movers(self.s.market.get("quote_asset", "USDT"), top_n)
            lines = [f"=== 24h 涨幅榜（Top {top_n}）==="]
            for i, r in enumerate(data["gainers"], 1):
                lines.append(f"{i:>2}. {r['symbol']:<14} {r['price']:<14g} {r['change_pct']:+.2f}%")
            lines.append("")
            lines.append(f"=== 24h 跌幅榜（Top {top_n}）===")
            for i, r in enumerate(data["losers"], 1):
                lines.append(f"{i:>2}. {r['symbol']:<14} {r['price']:<14g} {r['change_pct']:+.2f}%")
            return "\n".join(lines)
        except MarketError as e:
            return f"行情数据不可用：{e}"

    async def tool_report(self, symbol: str = "BTCUSDT") -> str:
        """Market report: price + technicals + order-book sentiment in one go."""
        parts = [f"{symbol} 市场报告 ({dt.datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]
        parts.append("— 价格与 24h 行情 —")
        parts.append(await self.tool_price(symbol))
        parts.append("")
        parts.append("— 技术信号 —")
        parts.append(await self.tool_signal(symbol))
        try:
            d = await self.market.depth(symbol, 20)
            bids = sum(float(p) * float(q) for p, q, *_ in d.get("bids", [])[:20])
            asks = sum(float(p) * float(q) for p, q, *_ in d.get("asks", [])[:20])
            ratio = bids / asks if asks else 0
            mood = "买盘占优，情绪偏多" if ratio > 1.15 else (
                "卖盘占优，情绪偏空" if ratio < 0.87 else "买卖均衡")
            parts += ["", "— 盘口情绪（前 20 档）—",
                      f"买卖深度比 {ratio:.2f}，{mood}"]
        except MarketError:
            pass
        return "\n".join(parts)

    async def tool_trade(self, text: str, source: str = "cli", confirm_cb=None) -> str:
        """自然语言 -> 交易意图 -> 风控 -> 下单。"""
        if source == "web":
            return "网页端已禁用真实交易（安全设计）。如需下单，请在命令行运行 `python -m cli chat`，并在 .env 中开启开关。"

        intent = await parse_intent(self.llm, text)
        req: Optional[TradeRequest] = None
        if intent and intent.get("action") in ("buy", "sell"):
            try:
                req = TradeRequest(
                    action=str(intent["action"]),
                    symbol=str(intent.get("symbol", "")).upper(),
                    quote_qty=float(intent.get("quote_qty", 0)),
                    source=source,
                )
            except (TypeError, ValueError):
                req = None
        if req is None:
            req = trade_request_from_text(text)
        if req is None:
            return (
                "未能从这句话中识别出交易意图。试试这种格式：`buy 50 USDT of BTC`，"
                "或先用 `python -m cli risk` 查看当前安全状态。"
            )
        if req.quote_qty == -1:
            # 数量为币本位：按最新价折算成计价金额
            try:
                price = float((await self.market.ticker_price(req.symbol))["price"])
                req.quote_qty = round((req.base_qty or 0.0) * price, 2)
            except MarketError:
                return "无法获取最新价格，不能把币数量折算成金额。请用 `buy 50 USDT of BTC` 这种格式。"

        result = await self.trader.place_order(req, confirm_cb)
        if result.get("status") == "REJECTED":
            return f"[已拒绝] 风控拦截了这笔订单：{result.get('reason')}\n\n当前安全状态：\n{self.risk.describe()}"
        if result.get("status") == "CANCELLED":
            return f"[已取消] {result.get('reason')}"
        o = result.get("order", {})
        env = result.get("env", "testnet")
        env_tag = "（模拟盘，虚拟资金）" if env == "testnet" else "（⚠ 正式盘，真实资金）"
        return (
            f"[已提交 → {env}{env_tag}]\n"
            f"  订单号：{o.get('orderId')} | 状态：{o.get('status')} | "
            f"方向：{o.get('side')} | 交易对：{o.get('symbol')} | 成交数量：{o.get('executedQty', '0')}\n"
            f"用 balances 查看最新资产。"
        )

    async def tool_balances(self) -> str:
        try:
            rows = await self.trader.balances_nonzero()
            if not rows:
                return "没有非零余额（或账户未授权）。"
            lines = ["资产        可用              冻结"]
            for b in rows[:20]:
                lines.append(f"{b['asset']:<10} {b['free']:<18} {b['locked']}")
            return "\n".join(lines)
        except Exception as e:
            return f"余额查询失败：{e}\n说明：需要有效的 API Key 且开通读取权限。"

    async def tool_confluence(self, symbol: str) -> str:
        """多周期共振信号（1h + 4h）。"""
        try:
            r = await self.market.confluence_signal(symbol, ("1h", "4h"), self.s.signals)
            lines = [f"{symbol} 多周期共振分析", ""]
            for iv, v in r["per"].items():
                lines.append(f"  [{iv}] 得分 {v['score']:+d}/4 | {v['verdict']} | MACD {v['macd']} | RSI {v['rsi']}")
            lines += [
                "",
                f"净分 {r['net']:+d} | {r['verdict']}" + ("（两周期同向，信号强度加成）" if r["agree"] else "（两周期方向分歧，谨慎）"),
                RISK_NOTE,
            ]
            return "\n".join(lines)
        except MarketError as e:
            return f"共振分析失败：{e}"

    async def tool_backtest(self, symbol: str, interval: str = "1h", limit: int = 1000) -> str:
        """历史回测：门限策略 vs 买入持有。"""
        from .backtest import run_backtest

        try:
            kl = await self.market.klines(symbol, interval, limit)
            r = run_backtest(symbol, kl, interval, 3, 0, 0.1, self.s.signals)
            return r.summary() + "\n\n" + RISK_NOTE
        except (MarketError, ValueError) as e:
            return f"回测失败：{e}"

    # ------------------------------------------------------------------
    # 自选列表（本地 JSON 持久化）
    # ------------------------------------------------------------------
    def _watch_path(self):
        from pathlib import Path

        return Path(__file__).resolve().parent.parent / "watchlist.json"

    def _load_watch(self) -> List[str]:
        import json

        p = self._watch_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
        return ["BTCUSDT", "ETHUSDT"]

    def _save_watch(self, items: List[str]) -> None:
        import json

        self._watch_path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    async def tool_watch(self, action: str, symbol: str = "") -> str:
        """自选列表管理：list / add / del。"""
        items = self._load_watch()
        if action == "add" and symbol:
            if symbol in items:
                return f"{symbol} 已在自选列表：{'、'.join(items)}"
            items.append(symbol)
            self._save_watch(items)
            return f"已添加 {symbol}。自选列表：{'、'.join(items)}"
        if action == "del" and symbol:
            if symbol not in items:
                return f"{symbol} 不在自选列表中。当前：{'、'.join(items)}"
            items.remove(symbol)
            self._save_watch(items)
            return f"已移除 {symbol}。自选列表：{'、'.join(items) or '（空）'}"
        lines = ["自选列表（add <币种> 添加 / del <币种> 移除）："]
        for s in items:
            try:
                t = await self.market.ticker_24h(s)
                lines.append(f"  {s:<14} {float(t['lastPrice']):,.6g}  {float(t['priceChangePercent']):+.2f}%")
            except MarketError:
                lines.append(f"  {s:<14} （数据不可用）")
        return "\n".join(lines)

    async def tool_orders(self, symbol: Optional[str] = None) -> str:
        """当前挂单列表（需要 Key）。"""
        try:
            opens = await self.trader.open_orders(symbol)
            lines = [f"=== 当前挂单（{len(opens)}）==="]
            for o in opens[:10]:
                lines.append(
                    f"#{o.get('orderId')} {o.get('symbol')} {o.get('side')} {o.get('type')} "
                    f"价 {o.get('price')} 量 {o.get('origQty')} 状态 {o.get('status')}"
                )
            if not opens:
                lines.append("（无挂单）")
            return "\n".join(lines)
        except Exception as e:
            return f"挂单查询失败：{e}\n（需要配置 Key 且开启下单开关）"

    async def tool_cancel(self, symbol: str, order_id: int) -> str:
        """撤销一笔挂单。"""
        try:
            r = await self.trader.cancel_order(symbol, order_id)
            return f"[已撤销] 订单号 {r.get('orderId')} 状态 {r.get('status')}"
        except Exception as e:
            return f"撤单失败：{e}"

    def tool_risk(self) -> str:
        return self.risk.describe()

    def tool_mcp(self) -> str:
        return mcp_full_guide()

    def tool_capabilities(self) -> str:
        m = capability_matrix(
            trade_ready=self.s.binance.keys_present,
            is_testnet=self.s.binance.is_testnet,
            llm_ready=self.llm.available and self.llm.s.provider != "mock",
            mcp_ready=False,
        )
        lines = [f"{self.s.agent_name} 能力清单", ""]
        for k, v in m.items():
            lines.append(f"  {k}：{v}")
        lines += ["", "支付工作流：" + payment_status(self.pay),
                  "链上自动化：" + onchain_status(self.onchain)]
        return "\n".join(lines)

    # ==================================================================
    # Conversation scheduling
    # ==================================================================
    async def chat(self, user_text: str) -> str:
        """一轮对话：优先规则引擎路由；LLM 可用时再走 LLM。"""
        text = user_text.strip()

        # 1) 规则引擎路由：显式命令/意图优先识别（精准、零成本）
        route = await self._route(text)
        if route is not None:
            return await route

        # 2) LLM mode: include history for multi-turn conversation (mock does not count as active)
        if self.llm.active:
            self.history.append({"role": "user", "content": text})
            try:
                reply = await self.llm.chat(self.history[-12:])
                self.history.append({"role": "assistant", "content": reply})
                return reply
            except LLMError as e:
                log.warning("LLM call failed: %s", e)
                return f"(LLM call failed: {e})\n\n{await self._fallback_answer(text)}"

        # 3) 纯规则引擎兜底
        return await self._fallback_answer(text)

    async def _route(self, text: str):
        """确定性命令优先路由：快且可靠。返回协程（调用方 await）。"""
        t = text.lower()

        async def _sync(fn, *a, **kw):
            return fn(*a, **kw)

        if any(k in t for k in ("行情报告", "报告")):
            return self.tool_report(await self._extract_symbol(text) or "BTCUSDT")
        if any(k in t for k in ("涨跌榜", "涨幅榜", "跌幅榜", "top", "movers")):
            return self.tool_movers()
        if any(k in t for k in ("信号", "分析", "rsi", "macd")):
            sym = await self._extract_symbol(text)
            if sym and any(k in t for k in ("共振", "多周期")):
                return self.tool_confluence(sym)
            return self.tool_signal(sym or "BTCUSDT")
        if t in ("价格", "行情") or any(k in t for k in ("价格", "多少钱")):
            return self.tool_price(await self._extract_symbol(text) or "BTCUSDT")
        if any(k in t for k in ("余额", "资产")):
            return self.tool_balances()
        if any(k in t for k in ("挂单", "订单")):
            return self.tool_orders(self._extract_symbol_sync(text))
        m_cancel = re.search(r"(?:撤单|撤销|cancel)\s*#?(\d+)", t)
        if m_cancel:
            sym = self._extract_symbol_sync(text) or "BTCUSDT"
            return self.tool_cancel(sym, int(m_cancel.group(1)))
        if any(k in t for k in ("回测", "backtest")):
            sym = await self._extract_symbol(text)
            m_iv = re.search(r"(15m|1h|4h|1d)", t)
            return self.tool_backtest(sym or "BTCUSDT", m_iv.group(1) if m_iv else "1h")
        if any(k in t for k in ("自选", "watchlist")):
            m_add = re.search(r"(?:添加|加入|add)\s*([A-Z]{2,10})(?:USDT)?", text.upper())
            m_del = re.search(r"(?:移除|删除|del|remove)\s*([A-Z]{2,10})(?:USDT)?", text.upper())
            if m_add:
                return self.tool_watch("add", m_add.group(1) + "USDT")
            if m_del:
                return self.tool_watch("del", m_del.group(1) + "USDT")
            return self.tool_watch("list")
        if any(k in t for k in ("风控", "安全状态")):
            return _sync(self.tool_risk)
        if any(k in t for k in ("mcp", "赛道二")):
            return _sync(self.tool_mcp)
        if any(k in t for k in ("能力", "你是谁", "介绍", "help")):
            return _sync(self.tool_capabilities)
        if any(k in t for k in ("买", "卖", "buy", "sell")) and any(ch.isdigit() for ch in t):
            return self.tool_trade(text, source="cli")
        return None

    @staticmethod
    def _extract_symbol_sync(text: str) -> Optional[str]:
        """同步版币种提取（内置常用名单 + USDT 后缀剥离），供订单路由用；查询类仍用动态校验版。"""
        m = re.search(r"(?<![A-Za-z0-9])([A-Z]{2,10})(?:USDT)?(?![A-Za-z0-9])", text.upper())
        if m and m.group(1) not in ("USDT", "USDC", "FDUSD"):
            return m.group(1) + "USDT"
        return None

    async def _fallback_answer(self, text: str) -> str:
        sym = await self._extract_symbol(text)
        if sym:
            return await self.tool_price(sym)
        return (
            f"我是 {self.s.agent_name}，一个加密市场行情助手。当前处于规则引擎模式（未配置 LLM Key），"
            "可以直接试：\n"
            "  • BTC 价格 / ETH 分析 / 行情报告\n"
            "  • 涨跌榜\n"
            "  • buy 50 USDT of BTC（下单去向由 .env 的 TRADE_BASE 决定：测试网/正式盘）\n"
            "  • 风控 / MCP / 能力\n"
            "在 .env 中填入 BIN_AGENT_LLM_API_KEY 后，就可以自由对话了。"
        )

    # 内置常用币种（离线兜底；在线时会用币安真实交易对列表动态校验）
    _COMMON_BASES = (
        "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON",
        "TRX", "DOT", "NEAR", "SUI", "PEPE", "SHIB", "ZEC", "LTC", "MATIC", "ARB",
        "OP", "APT", "FIL", "ATOM", "ETC", "UNI", "AAVE", "INJ", "TIA", "SEI",
    )

    async def _extract_symbol(self, text: str) -> Optional[str]:
        """从文本提取币种：优先用币安真实交易对动态校验，离线时回退内置列表。"""
        import re

        # 候选词：连续字母（2-12 位，允许 BTCUSDT 这种带后缀写法），左右不能是字母数字（允许紧跟中文）
        words = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{2,12}(?![A-Za-z0-9])", text.upper())
        candidates = []
        for w in words:
            for suffix in ("USDT", "USDC", "FDUSD"):
                if w.endswith(suffix) and len(w) > len(suffix):
                    w = w[: -len(suffix)]
                    break
            if w and w not in ("USDT", "USDC", "FDUSD"):
                candidates.append(w)

        valid = await self.market.get_valid_bases()
        if valid is None:
            valid = set(self._COMMON_BASES)
        valid_lower = {b.lower() for b in valid}

        for w in candidates:
            if w in valid or w.lower() in valid_lower:
                return w.upper() + "USDT"
        return None


RISK_NOTE = "不构成投资建议。加密资产波动剧烈，请控制仓位与风险。"
