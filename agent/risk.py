"""风控安全层。

设计原则（默认拒绝）：
1. 任何下单请求必须先经过本层审查；
2. 任一条件不满足即拒绝；
3. Agent（LLM）永远不被直接信任决定交易参数——一切在这里重新校验；
4. 提币/划转在永久黑名单，不受任何开关影响。
5. 下单去向由 .env 的 BIN_AGENT_BINANCE_TRADE_BASE 决定：
   测试网（虚拟资金，随便玩）/ 正式盘（真钱，执行层强制逐笔人工确认）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import AgentSettings

log = logging.getLogger("agent.risk")

# 硬禁止操作：任何模式下都拒绝
HARD_FORBIDDEN = {"withdraw", "transfer", "withdrawal", "deposit-fiat"}


@dataclass
class TradeRequest:
    """一笔交易请求（无论来自用户命令还是 LLM 意图解析，统一走这里）。"""
    action: str                       # buy / sell
    symbol: str                       # 例如 BTCUSDT
    quote_qty: float                  # 计价币金额（USDT）；-1 表示待按币数量折算
    price: Optional[float] = None     # 限价；None = 市价单
    source: str = "cli"               # cli / web / llm
    base_qty: Optional[float] = None  # 币数量（与 quote_qty=-1 配合使用）


@dataclass
class RiskDecision:
    allowed: bool
    reason: str


class RiskGuard:
    def __init__(self, settings: AgentSettings):
        self.s = settings

    def check(self, req: TradeRequest) -> RiskDecision:
        s = self.s.risk

        # 1) 硬黑名单——优先级最高
        if req.action.lower() in HARD_FORBIDDEN:
            return RiskDecision(False, f"操作 {req.action!r} 在硬禁止名单中，任何开关无法解除")

        # 2) 操作白名单
        if req.action.lower() not in {"buy", "sell"}:
            return RiskDecision(False, f"不支持的操作：{req.action}")

        # 3) 下单总开关（测试网/正式盘共用）
        if not s.trading_enabled:
            return RiskDecision(
                False,
                "下单总开关 BIN_AGENT_TRADING_ENABLED=false，请在 .env 中开启后重试"
                "（测试网模拟盘也需要打开，下单才会真正发到测试网撮合）",
            )

        # 4) Web 前端永远禁止下单（防网页误触）
        if req.source == "web" and not self.s.web.allow_real_trades:
            return RiskDecision(False, "网页端已硬编码禁用下单，请在命令行（python -m cli）中操作交易")

        # 5) 币种黑名单与格式
        sym = req.symbol.upper().strip()
        if sym in s.symbol_blacklist:
            return RiskDecision(False, f"{sym} 在禁止币种黑名单中")
        if not sym.isalnum():
            return RiskDecision(False, "交易对格式不合法")
        if not sym.endswith(("USDT", "USDC", "FDUSD", "BUSD")):
            return RiskDecision(False, "仅支持稳定币计价交易对（USDT/USDC/FDUSD），避免高风险杠杆对")

        # 6) 单笔限额
        if req.quote_qty <= 0:
            return RiskDecision(False, "下单金额必须大于 0")
        if req.quote_qty > s.max_order_notional:
            return RiskDecision(
                False,
                f"下单金额 {req.quote_qty} USDT 超过单笔限额 {s.max_order_notional} USDT（BIN_AGENT_MAX_ORDER_NOTIONAL）",
            )

        # 7) 环境检查：测试网与正式盘都需要 Key
        if not self.s.binance.keys_present:
            if self.s.binance.is_testnet:
                return RiskDecision(
                    False,
                    "测试网也需要 Key：到 https://testnet.binance.vision 用 GitHub 登录生成，填入 .env 后重试",
                )
            return RiskDecision(False, "正式盘下单需要 BIN_AGENT_BINANCE_API_KEY / API_SECRET（建议子账户，不要开提币权限）")

        env = "模拟盘 testnet（虚拟资金）" if self.s.binance.is_testnet else "正式盘（真实资金）"
        return RiskDecision(True, f"风控通过，将下单到【{env}】")

    def describe(self) -> str:
        """返回可读的安全状态摘要。"""
        s = self.s.risk
        env = "模拟盘（testnet，虚拟资金）" if self.s.binance.is_testnet else "正式盘（真钱）"
        master = "已开启" if s.trading_enabled else "关闭"
        return (
            f"交易环境：{env} | 下单开关：{master} | "
            f"单笔限额：{s.max_order_notional} USDT | "
            f"禁止币种：{','.join(s.symbol_blacklist) or '无'} | "
            f"硬禁止操作：提币/划转（任何开关不可解除）"
            + ("" if self.s.binance.is_testnet else " | 正式盘每笔下单需 CLI 手动确认")
        )
