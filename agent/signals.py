"""Technical indicators and strategy signal engine (pure local computation, no dependencies).

Contains: SMA/EMA/RSI/MACD/Bollinger Bands, plus a composite signal "score".
All input is closing-price sequences; output is structured signals, for the Agent and reports to reference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


# ----------------------------------------------------------------------
# Basic indicators
# ----------------------------------------------------------------------
def sma(closes: List[float], period: int) -> List[float]:
    out: List[float] = []
    for i in range(len(closes)):
        if i + 1 < period:
            out.append(float("nan"))
        else:
            out.append(sum(closes[i + 1 - period : i + 1]) / period)
    return out


def ema(closes: List[float], period: int) -> List[float]:
    out: List[float] = []
    k = 2.0 / (period + 1)
    prev = float("nan")
    for i, c in enumerate(closes):
        if i + 1 < period:
            out.append(float("nan"))
            continue
        if prev != prev:  # first valid value = SMA
            prev = sum(closes[:period]) / period
        else:
            prev = c * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: List[float], period: int = 14) -> List[float]:
    out: List[float] = [float("nan")] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(
    closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[List[float], List[float], List[float]]:
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [f - s if f == f and s == s else float("nan") for f, s in zip(ef, es)]
    valid = [x for x in dif if x == x]
    dea_valid = ema(valid, signal)
    dea = [float("nan")] * (len(dif) - len(dea_valid)) + dea_valid
    hist = [d - g if d == d and g == g else float("nan") for d, g in zip(dif, dea)]
    return dif, dea, hist


def bollinger(closes: List[float], period: int = 20, stddev: float = 2.0):
    mid = sma(closes, period)
    up, low = list(mid), list(mid)
    for i in range(len(closes)):
        if i + 1 < period:
            continue
        window = closes[i + 1 - period : i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        up[i], low[i] = m + stddev * sd, m - stddev * sd
    return mid, up, low


# ----------------------------------------------------------------------
# Composite signal
# ----------------------------------------------------------------------
@dataclass
class SignalReport:
    symbol: str
    interval: str
    price: float
    ma_state: str = "neutral"        # bullish / bearish / neutral
    rsi_value: float = float("nan")
    rsi_state: str = "neutral"       # oversold / overbought / neutral
    macd_state: str = "neutral"      # golden_cross / death_cross / bullish / bearish / neutral
    boll_state: str = "neutral"      # breakout_up / breakout_down / neutral
    score: int = 0                   # -4..+4, larger means more bullish
    summary: str = ""
    details: Dict[str, str] = field(default_factory=dict)

    def verdict(self) -> str:
        if self.score >= 3:
            return "强势看涨"
        if self.score >= 1:
            return "谨慎看涨"
        if self.score == 0:
            return "中性观望"
        if self.score > -3:
            return "谨慎看跌"
        return "强势看跌"


def analyze(
    symbol: str,
    closes: List[float],
    interval: str = "1h",
    params: Dict | None = None,
) -> SignalReport:
    """Feed in the closing-price sequence; output a composite signal for four indicator families."""
    p = params or {}
    ma_fast = int(p.get("ma", {}).get("fast", 7))
    ma_slow = int(p.get("ma", {}).get("slow", 25))
    rsi_period = int(p.get("rsi", {}).get("period", 14))
    rsi_os = float(p.get("rsi", {}).get("oversold", 30))
    rsi_ob = float(p.get("rsi", {}).get("overbought", 70))
    macd_p = p.get("macd", {})
    boll_p = p.get("bollinger", {})

    if len(closes) < max(ma_slow, rsi_period + 1, 35):
        raise ValueError(f"Insufficient sample: need at least {max(ma_slow, rsi_period + 1, 35)} candles, got {len(closes)}")

    rep = SignalReport(symbol=symbol, interval=interval, price=closes[-1])
    score = 0

    # --- MA 多空排列 ---
    ef, es = ema(closes, ma_fast)[-1], ema(closes, ma_slow)[-1]
    if ef > es:
        rep.ma_state = "bullish"
        score += 1
        rep.details["均线"] = f"EMA{ma_fast}({ef:.4f}) 上穿 EMA{ma_slow}({es:.4f})，多头排列"
    elif ef < es:
        rep.ma_state = "bearish"
        score -= 1
        rep.details["均线"] = f"EMA{ma_fast}({ef:.4f}) 下穿 EMA{ma_slow}({es:.4f})，空头排列"
    else:
        rep.details["均线"] = "短期与长期均线粘合，方向不明"

    # --- RSI ---
    r = rsi(closes, rsi_period)[-1]
    rep.rsi_value = r
    if r != r:
        rep.details["RSI"] = "数据不足"
    elif r <= rsi_os:
        rep.rsi_state = "oversold"
        score += 1
        rep.details["RSI"] = f"RSI={r:.1f}，进入超卖区，关注反弹但提防续跌"
    elif r >= rsi_ob:
        rep.rsi_state = "overbought"
        score -= 1
        rep.details["RSI"] = f"RSI={r:.1f}，进入超买区，注意回调风险"
    else:
        rep.details["RSI"] = f"RSI={r:.1f}，中性区间"

    # --- MACD histogram ---
    dif, dea, hist = macd(
        closes,
        int(macd_p.get("fast", 12)),
        int(macd_p.get("slow", 26)),
        int(macd_p.get("signal", 9)),
    )
    h_now, h_prev = hist[-1], hist[-2]
    if h_now == h_now and h_prev == h_prev:
        if h_prev <= 0 < h_now:
            rep.macd_state = "golden_cross"
            score += 2
            rep.details["MACD"] = "柱状图由负转正（金叉），动能转强"
        elif h_prev >= 0 > h_now:
            rep.macd_state = "death_cross"
            score -= 2
            rep.details["MACD"] = "柱状图由正转负（死叉），动能转弱"
        else:
            rep.macd_state = "bullish" if h_now > 0 else "bearish"
            score += 1 if h_now > 0 else -1
            rep.details["MACD"] = f"柱状图在零轴上方（{h_now:.6f}）" if h_now > 0 else f"柱状图在零轴下方（{h_now:.6f}）"
    else:
        rep.details["MACD"] = "数据不足"

    # --- Bollinger position ---
    mid, up, low = bollinger(
        closes, int(boll_p.get("period", 20)), float(boll_p.get("stddev", 2))
    )
    price = closes[-1]
    if price >= up[-1]:
        rep.boll_state = "breakout_up"
        score += 1
        rep.details["布林带"] = f"价格 {price:.4f} 突破上轨 {up[-1]:.4f}，动能强但短线乖离偏大"
    elif price <= low[-1]:
        rep.boll_state = "breakout_down"
        score -= 1
        rep.details["布林带"] = f"价格 {price:.4f} 跌破下轨 {low[-1]:.4f}，恐慌抛售或趋势破位"
    else:
        rep.details["布林带"] = f"价格在轨道内 [{low[-1]:.4f}, {up[-1]:.4f}]"

    rep.score = max(-4, min(4, score))
    _st_cn = {
        "bullish": "偏多", "bearish": "偏空", "neutral": "中性",
        "oversold": "超卖", "overbought": "超买",
        "golden_cross": "金叉", "death_cross": "死叉",
        "breakout_up": "突破上轨", "breakout_down": "跌破下轨",
    }
    cn = lambda s: _st_cn.get(s, s)
    rep.summary = (
        f"{symbol}（{interval}）最新价 {price:.6g}，综合信号【{rep.verdict()}】（得分 {rep.score:+d}/4）："
        f"MA {cn(rep.ma_state)} | RSI {cn(rep.rsi_state)} | MACD {cn(rep.macd_state)} | 布林 {cn(rep.boll_state)}"
    )
    return rep
