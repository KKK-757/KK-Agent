"""Risk-control layer unit tests: verifying "by default absolutely no real orders"."""
import asyncio

from agent.config import AgentSettings, BinanceConfig, RiskConfig, WebConfig
from agent.risk import RiskGuard, TradeRequest


def make_guard(**risk_kw) -> RiskGuard:
    s = AgentSettings(
        binance=BinanceConfig(api_key="test", api_secret="test"),
        risk=RiskConfig(**risk_kw),
        web=WebConfig(allow_real_trades=False),
    )
    return RiskGuard(s)


def test_default_blocks_all_orders():
    """出厂默认状态：下单总开关关闭，测试网/正式盘都被拒绝。"""
    g = make_guard()  # trading_enabled=False
    d = g.check(TradeRequest("buy", "BTCUSDT", 20))
    assert not d.allowed
    assert "BIN_AGENT_TRADING_ENABLED" in d.reason


def test_testnet_order_passes():
    """测试网 + 开关开 + 有 Key：放行，发往模拟盘。"""
    g = make_guard(trading_enabled=True)
    d = g.check(TradeRequest("buy", "BTCUSDT", 20))
    assert d.allowed
    assert "模拟盘" in d.reason


def test_testnet_requires_keys():
    """测试网也需要 Key 才能下单。"""
    s = AgentSettings(
        binance=BinanceConfig(),  # 无 Key
        risk=RiskConfig(trading_enabled=True),
    )
    d = RiskGuard(s).check(TradeRequest("buy", "BTCUSDT", 20))
    assert not d.allowed
    assert "testnet.binance.vision" in d.reason


def test_mainnet_order_passes_with_keys():
    """正式盘 + 开关开 + 有 Key：风控放行（人工确认由执行层强制）。"""
    s = AgentSettings(
        binance=BinanceConfig(api_key="k", api_secret="s", trade_base="https://api.binance.com"),
        risk=RiskConfig(trading_enabled=True),
    )
    d = RiskGuard(s).check(TradeRequest("buy", "BTCUSDT", 20))
    assert d.allowed
    assert "正式盘" in d.reason


def test_hard_forbidden_withdraw():
    """提币任何模式下都拒绝——即使全部开关打开。"""
    g = make_guard(trading_enabled=True)
    d = g.check(TradeRequest("withdraw", "BTCUSDT", 20))
    assert not d.allowed
    assert "硬禁止名单" in d.reason


def test_web_always_blocked():
    """Web 来源始终拒绝真实下单（防网页误触真钱）。"""
    g = make_guard(trading_enabled=True)
    d = g.check(TradeRequest("buy", "BTCUSDT", 20, source="web"))
    assert not d.allowed


def test_notional_limit():
    """超过单笔限额的订单被拒绝。"""
    g = make_guard(trading_enabled=True, max_order_notional=100)
    d = g.check(TradeRequest("buy", "BTCUSDT", 500))
    assert not d.allowed
    assert "超过单笔限额" in d.reason


def test_blacklist_symbol():
    g = make_guard(trading_enabled=True, symbol_blacklist=("PEPEUSDT",))
    d = g.check(TradeRequest("buy", "PEPEUSDT", 10))
    assert not d.allowed


def test_non_stable_quote_rejected():
    """只允许稳定币计价对；BTCBNB/ETHBTC 这类高风险保证金对被拒绝。"""
    g = make_guard(trading_enabled=True)
    d = g.check(TradeRequest("buy", "ETHBTC", 10))
    assert not d.allowed


def test_dry_run_field_removed():
    """dry_run 概念已移除：RiskDecision.dry_run 保留兼容但不再用于拦截判断。"""
    g = make_guard(trading_enabled=True)
    d = g.check(TradeRequest("buy", "BTCUSDT", 20))
    assert d.allowed


def test_real_trade_requires_keys():
    """正式盘下单必须有 Key。"""
    s = AgentSettings(
        binance=BinanceConfig(trade_base="https://api.binance.com"),  # 无 Key，正式盘
        risk=RiskConfig(trading_enabled=True),
    )
    d = RiskGuard(s).check(TradeRequest("buy", "BTCUSDT", 20))
    assert not d.allowed
    assert "API_KEY" in d.reason


def test_real_trade_pass():
    g = make_guard(trading_enabled=True)
    d = g.check(TradeRequest("buy", "BTCUSDT", 20))
    assert d.allowed
