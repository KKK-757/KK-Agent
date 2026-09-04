"""Payment workflow (x402) and on-chain automation module.

Design notes:
- These two directions belong to the Binance Agent OS "Wallet Agentic Hub / x402 programmable payment" capability.
  They rely on the Binance Agentic Wallet (a wallet with per-action limits) and an on-chain environment,
  and cannot run locally without wallet authorization.
- This module provides: capability descriptions, interface contracts, configuration generators, and status checks,
  forming a complete "pluggable" architecture: once the wallet authorization flow is ready, it plugs in here,
  while the Agent's other modules (market data/analysis/spot trading) work independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


# ----------------------------------------------------------------------
# x402 programmable payment
# ----------------------------------------------------------------------
X402_INFO = {
    "what": "x402 is Binance's programmable payment protocol based on HTTP 402, allowing AI Agents to make automatic payments per API call/task",
    "scenario": "Agent-to-Agent payments, pay-per-use data/API subscriptions, automated settlement",
    "prereq": "A Binance Agentic Wallet must be created and a per-transaction/per-day payment limit set",
    "docs": "https://developers.binance.com/en/docs/agent-native (x402 / Agentic Wallet section)",
}


@dataclass
class PaymentCapability:
    """Payment capability declaration (exposed to the Agent and demos)."""
    name: str = "x402-pay"
    enabled: bool = False          # Disabled by default until wallet authorization is configured
    wallet_bound: bool = False
    per_tx_limit_usdc: float = 0.0
    per_day_limit_usdc: float = 0.0
    allowed_payees: List[str] = field(default_factory=list)   # Whitelisted payees


def payment_status(cap: PaymentCapability) -> str:
    if cap.enabled and cap.wallet_bound:
        return (
            f"x402 支付已启用 | 单笔限额 {cap.per_tx_limit_usdc} USDC | "
            f"日限额 {cap.per_day_limit_usdc} USDC | 收款白名单：{', '.join(cap.allowed_payees) or '无'}"
        )
    return (
        "x402 支付未配置（安全默认）。启用方式：在币安 App 中创建 Agentic 钱包并设置较低的单笔限额，"
        f"然后在此绑定。文档：{X402_INFO['docs']}"
    )


# ----------------------------------------------------------------------
# On-chain automation
# ----------------------------------------------------------------------
ONCHAIN_INFO = {
    "what": "Connecting the AI Agent to a wallet to execute supported on-chain transactions (staking, DeFi, transfers) within set limits",
    "components": "Binance Wallet Agentic Hub + Web3 APIs",
    "prereq": "Binance Web3 wallet and Agentic authorization; on-chain gas is paid by the wallet",
    "docs": "https://www.binance.com/zh-CN/agent-os (on-chain automation section)",
}


@dataclass
class OnchainCapability:
    """On-chain capability declaration."""
    name: str = "onchain-automation"
    enabled: bool = False
    wallet_bound: bool = False
    allowed_chains: List[str] = field(default_factory=lambda: ["BNB Chain", "Ethereum"])
    allowed_actions: List[str] = field(default_factory=lambda: ["stake", "defi-swap"])
    max_gas_price_gwei: float = 5.0


def onchain_status(cap: OnchainCapability) -> str:
    if cap.enabled and cap.wallet_bound:
        return (
            f"链上自动化已启用 | 链：{', '.join(cap.allowed_chains)} | "
            f"允许操作：{', '.join(cap.allowed_actions)} | Gas 上限 {cap.max_gas_price_gwei} Gwei"
        )
    return (
        "链上自动化未配置（安全默认）。启用方式：在 Agent OS 中绑定币安 Web3 钱包，"
        f"设置操作白名单和 Gas 限额。文档：{ONCHAIN_INFO['docs']}"
    )


# ----------------------------------------------------------------------
# Unified capability inventory (for the Agent's self-introduction and Demo)
# ----------------------------------------------------------------------
def capability_matrix(
    trade_ready: bool, is_testnet: bool, llm_ready: bool, mcp_ready: bool
) -> Dict[str, str]:
    env = "模拟盘 testnet（虚拟资金）" if is_testnet else "正式盘（真钱，逐笔确认）"
    trade = f"已连接 {env}" if trade_ready else "未配置 Key（.env 中填入并开开关）"
    return {
        "数据与分析": "可用：实时价格、K线、深度、涨跌榜、市场报告",
        "交易工作流": trade,
        "支付工作流 (x402)": "接口就绪，待绑定 Agentic 钱包",
        "链上自动化": "接口就绪，待绑定 Web3 钱包",
        "LLM 推理": "规则引擎模式（在 .env 填 BIN_AGENT_LLM_API_KEY 后升级）" if not llm_ready else "已配置",
        "币安 MCP": "端点已配置，未连接（运行 `python -m cli mcp` 查看接入指引）",
    }
