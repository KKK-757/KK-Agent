"""简易历史回测引擎。

策略：基于复合信号得分的门限策略——
  得分 >= buy_threshold 时满仓买入，<= exit_threshold（默认 0）时清仓，空仓期不动。
逐根滚动计算信号（只用当根及之前的数据，无未来函数），输出收益曲线和关键统计。

数据来自币安公开K线接口，无需 Key。仅用于策略思路验证，不构成任何收益承诺。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List

from . import signals as _signals

log = logging.getLogger("agent.backtest")


@dataclass
class BacktestResult:
    symbol: str
    interval: str
    start_price: float
    end_price: float
    buy_hold_pct: float            # 买入持有收益 %
    strategy_pct: float            # 策略收益 %
    trades: int
    win_trades: int
    max_drawdown_pct: float        # 最大回撤 %
    days: int
    equity: List[float] = field(default_factory=list)   # 策略净值序列
    bench: List[float] = field(default_factory=list)    # 买入持有净值序列
    events: List[str] = field(default_factory=list)     # 最近买卖事件

    @property
    def win_rate(self) -> float:
        return (self.win_trades / self.trades * 100) if self.trades else 0.0

    @property
    def outperformance(self) -> float:
        """相对买入持有的超额收益（百分点）。"""
        return self.strategy_pct - self.buy_hold_pct

    def summary(self) -> str:
        lines = [
            f"{self.symbol} 回测（{self.interval}，近 {self.days} 天，手续费 0.1%/次）",
            f"  策略收益：{self.strategy_pct:+.2f}%   买入持有：{self.buy_hold_pct:+.2f}%   超额：{self.outperformance:+.2f}pp",
            f"  交易 {self.trades} 次 | 胜率 {self.win_rate:.0f}% | 最大回撤 {self.max_drawdown_pct:.2f}%",
        ]
        lines += [f"  {e}" for e in self.events[-8:]]
        return "\n".join(lines)


def run_backtest(
    symbol: str,
    klines: List[List],
    interval: str = "1h",
    buy_threshold: int = 3,
    exit_threshold: int = 0,
    fee_pct: float = 0.1,
    signal_params: Dict | None = None,
) -> BacktestResult:
    """滚动回测。klines 为币安格式，需足够长（60 根预热 + 回测区间）。"""
    closes = [float(k[4]) for k in klines]
    times = [int(k[0]) for k in klines]
    n = len(closes)
    if n < 80:
        raise ValueError(f"K线数量不足（{n} 根），至少需要 80 根（预热 + 回测区间）")

    warmup = 60
    cash, pos = 1.0, 0.0
    equity: List[float] = []
    bench: List[float] = []
    events: List[str] = []
    trades = win_trades = 0
    last_flat_nav = 1.0
    peak, max_dd = 1.0, 0.0
    fee = fee_pct / 100.0

    for i in range(warmup, n):
        price = closes[i]
        # 滚动信号：切片只含当根及之前（无未来函数）
        try:
            rep = _signals.analyze(symbol, closes[: i + 1], interval, signal_params or {})
            score = rep.score
        except ValueError:
            score = 0

        if pos == 0.0 and score >= buy_threshold:
            pos = (1.0 - fee) / price
            cash = 0.0
            trades += 1
            events.append(f"买入 @ {price:.6g}（信号 {score:+d}）")
        elif pos > 0.0 and score <= exit_threshold:
            cash = pos * price * (1.0 - fee)
            if cash >= last_flat_nav:
                win_trades += 1
            last_flat_nav = cash
            pos = 0.0
            events.append(f"卖出 @ {price:.6g}（净值 {cash:.4f}）")

        nav = cash + pos * price
        equity.append(nav)
        bench.append(price / closes[warmup])
        peak = max(peak, nav)
        max_dd = max(max_dd, (peak - nav) / peak)

    end_nav = equity[-1] if equity else 1.0
    start_price = closes[warmup]
    days = int((times[-1] - times[warmup]) / 86400000)

    return BacktestResult(
        symbol=symbol,
        interval=interval,
        start_price=start_price,
        end_price=closes[-1],
        buy_hold_pct=(closes[-1] / start_price - 1) * 100,
        strategy_pct=(end_nav - 1) * 100,
        trades=trades,
        win_trades=win_trades,
        max_drawdown_pct=max_dd * 100,
        days=days,
        equity=equity,
        bench=bench,
        events=events,
    )
