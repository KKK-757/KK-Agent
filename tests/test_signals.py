"""Signal engine unit tests.

Design principles: synthetic candle sequences cannot simultaneously pin all four indicator families to a
specified state (e.g. a straight-up move is bound to trigger RSI overbought deductions), so we adopt three robust
test approaches:
1. Mirror symmetry: strictly opposite up/down differences necessarily yield opposite scores;
2. Component-level assertions: test one indicator family at a time;
3. V-reversal: a deep drop followed by a sustained rise necessarily produces a MACD golden cross.
"""
from agent.signals import SignalReport, analyze, ema, macd, rsi, sma
import pytest


def _series(diffs, start=100.0):
    """Cumulative close sequence constructed from a per-bar difference sequence."""
    closes, p = [], start
    for d in diffs:
        p += d
        closes.append(p)
    return closes


def _cycle_diffs(n=120):
    """Uptrend with pullbacks: net rise per 5 bars, alternating gains and losses (realistic feel)."""
    cycle = [1.0, -0.6, 0.9, -0.5, 0.7]  # +1.5 net rise per cycle
    return [cycle[i % 5] for i in range(n)]


def test_indicators_shapes():
    c = _series(_cycle_diffs(50))
    assert len(sma(c, 7)) == 50
    assert len(ema(c, 7)) == 50
    assert len(rsi(c, 14)) == 50
    dif, dea, hist = macd(c)
    assert len(dif) == len(dea) == len(hist) == 50


def test_mirror_symmetry():
    """Strictly opposite up/down differences necessarily yield opposite scores and mirrored MA states."""
    up = _series(_cycle_diffs())
    down = _series([-d for d in _cycle_diffs()], start=300.0)
    ru = analyze("TUSDT", up, "1h", {})
    rd = analyze("TUSDT", down, "1h", {})
    assert ru.score == -rd.score
    assert ru.ma_state == "bullish" and rd.ma_state == "bearish"
    assert ru.rsi_value == pytest.approx(100 - rd.rsi_value)  # Mirrored RSI (floating-point tolerance)


def test_uptrend_not_bearish():
    """An uptrend with pullbacks must not receive a bearish verdict (weak but robust assertion)."""
    rep = analyze("TUSDT", _series(_cycle_diffs()), "1h", {})
    assert rep.score > -4
    assert "Strongly bearish" not in rep.verdict()
    assert rep.ma_state == "bullish"


def test_ma_bullish_on_uptrend():
    rep = analyze("TUSDT", _series(_cycle_diffs()), "1h", {})
    assert rep.ma_state == "bullish"


def test_rsi_overbought_on_strong_rally():
    """A straight-up move necessarily triggers overbought."""
    c = _series([0.8] * 60)
    val = rsi(c, 14)[-1]
    assert val > 70
    rep = analyze("TUSDT", c, "1h", {})
    assert rep.rsi_state == "overbought"


def test_rsi_oversold_on_slide():
    """A straight-down move necessarily triggers oversold."""
    c = _series([-0.8] * 60)
    val = rsi(c, 14)[-1]
    assert val < 30
    rep = analyze("TUSDT", c, "1h", {})
    assert rep.rsi_state == "oversold"


def test_v_reversal_golden_cross():
    """Deep drop followed by sustained rise: the end state must be a MACD golden cross or bullish, with overall verdict bullish."""
    diffs = [-0.5] * 70 + [0.5] * 50
    rep = analyze("TUSDT", _series(diffs), "1h", {})
    assert rep.macd_state in ("golden_cross", "bullish")
    assert rep.score > 0


def test_bollinger_breakout_up():
    """Sustained range-bound moves followed by a sharp rally (accelerated move) necessarily break above the upper band.

    Note: in a purely linear uptrend the band width widens in sync with sigma, so the price never touches the
    upper band — a breakout must be constructed from an 'accelerated' price pattern.
    """
    c = _series([0.01] * 52 + [2.0] * 8)
    rep = analyze("TUSDT", c, "1h", {})
    assert rep.boll_state == "breakout_up"


def test_insufficient_data_raises():
    try:
        analyze("TUSDT", [1, 2, 3], "1h", {})
        assert False, "should have raised"
    except ValueError as e:
        assert "Insufficient sample" in str(e)


def test_verdict_mapping():
    """各得分档位的结论文案映射。"""
    def v(score):
        r = SignalReport(symbol="T", interval="1h", price=1)
        r.score = score
        return r.verdict()
    assert v(4) == "强势看涨"
    assert v(2) == "谨慎看涨"
    assert v(0) == "中性观望"
    assert v(-2) == "谨慎看跌"
    assert v(-4) == "强势看跌"
