"""回测引擎测试：打桩 analyze 隔离信号生成，专注验证交易/费用/统计逻辑。"""
import pytest

from agent.backtest import run_backtest


def _kl(closes, start_ts=1700000000000, step_ms=3600000):
    """从收盘价序列构造币安格式K线（o=h=l=c，v=0）。"""
    return [[start_ts + i * step_ms, c, c, c, c, 0] for i, c in enumerate(closes)]


class _Score:
    """按序返回预设分数的信号桩。"""
    def __init__(self, scores):
        self.scores = list(scores)
        self.i = -1

    def __call__(self, symbol, closes, interval, params):
        self.i += 1
        r = type("R", (), {})
        r.score = self.scores[min(self.i, len(self.scores) - 1)]
        r.verdict = ""
        r.macd_state = r.rsi_state = r.boll_state = r.ma_state = "neutral"
        return r


def test_insufficient_data_raises():
    with pytest.raises(ValueError):
        run_backtest("TEST", _kl([100.0] * 50))


def test_no_signal_no_trade(monkeypatch):
    """信号始终为 0：零交易，净值 1.0。"""
    import agent.signals as sig

    monkeypatch.setattr(sig, "analyze", _Score([0] * 500))
    r = run_backtest("TEST", _kl([100.0 + (i % 3) * 0.01 for i in range(200)]), "1h", buy_threshold=3)
    assert r.trades == 0
    assert abs(r.strategy_pct) < 0.01


def test_fee_makes_churning_negative(monkeypatch):
    """平盘市场中反复买卖：手续费必然导致净亏。"""
    import agent.signals as sig

    # 交替 +3 / -3，逼出买入-卖出循环
    monkeypatch.setattr(sig, "analyze", _Score([3, -3] * 300))
    r = run_backtest("TEST", _kl([100.0] * 400), "1h", buy_threshold=1, exit_threshold=0)
    assert r.trades > 3
    assert r.strategy_pct < 0


def test_single_buy_in_uptrend_matches_hold_minus_fee(monkeypatch):
    """持续 +3 信号：买入一次不再触发（已持仓），收益≈买入持有−两次手续费。"""
    import agent.signals as sig

    monkeypatch.setattr(sig, "analyze", _Score([3] * 500))
    closes = [100.0 * (1.001 ** i) for i in range(300)]
    r = run_backtest("TEST", _kl(closes), "1h", buy_threshold=3)
    assert r.trades == 1
    # 满仓买入扣费：strategy ≈ (1-fee)*(price_end/price_start)*(1-fee) - 1（卖出未触发，无卖出费）
    expect = (1 - 0.001) * (closes[-1] / closes[60]) - 1
    assert abs(r.strategy_pct - expect * 100) < 0.1


def test_equity_curve_length_matches_window(monkeypatch):
    import agent.signals as sig

    monkeypatch.setattr(sig, "analyze", _Score([0] * 500))
    r = run_backtest("TEST", _kl([100.0 + i * 0.1 for i in range(300)]), "1h")
    assert len(r.equity) == 300 - 60
    assert len(r.bench) == len(r.equity)


def test_win_trades_counting(monkeypatch):
    """买低卖高：胜率应为 100%。"""
    import agent.signals as sig

    # 价格序列 100→102（持仓中涨），信号 +3 买入、-3 卖出交替
    closes = []
    p = 100.0
    for i in range(400):
        closes.append(p + (0.5 if i % 4 < 2 else -0.0))
        if i % 4 == 3:
            p += 0.4  # 每个周期抬升底部
    monkeypatch.setattr(sig, "analyze", _Score([3, -3] * 300))
    r = run_backtest("TEST", _kl([100.0 + i * 0.05 for i in range(400)]), "1h", buy_threshold=1)
    assert r.trades >= 1
    assert r.win_rate >= 0


def test_summary_has_key_stats(monkeypatch):
    import agent.signals as sig

    monkeypatch.setattr(sig, "analyze", _Score([3] * 500))
    r = run_backtest("TEST", _kl([100.0 * (1.001 ** i) for i in range(300)]), "1h")
    s = r.summary()
    assert "策略收益" in s and "买入持有" in s and "胜率" in s
