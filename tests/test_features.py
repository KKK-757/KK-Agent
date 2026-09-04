"""共振信号与自选列表测试（K线与分析函数用 monkeypatch 打桩，分层验证聚合逻辑）。"""
import asyncio
import json

import pytest

from agent.core import BinanceAgent
from agent.market import MarketData


def _fake_klines(closes):
    """币安格式：[openTime, o, h, l, c, v] —— 之前漏了 openTime 导致 6 列错位。"""
    return [[0, c, c, c, c, 0] for c in closes]


def _md_with_klines(seqs: dict) -> MarketData:
    md = MarketData.__new__(MarketData)

    async def fake_klines(symbol, interval, limit):
        rows = seqs[interval]
        # 每行插入 openTime 后即为币安格式
        return [[i * 3600000] + row for i, row in enumerate(rows)]

    md.klines = fake_klines
    return md


def test_confluence_agreement_amplifies(monkeypatch):
    """两周期同向：agree=True 且净分被 1.5x 放大（打桩 analyze 隔离方向计算）。"""
    import agent.signals as sig

    md = _md_with_klines({"1h": _fake_klines([100] * 120), "4h": _fake_klines([100] * 120)})

    class FakeRep:
        score = 2
        macd_state = "bullish"
        rsi_state = "neutral"
        boll_state = "neutral"
        ma_state = "bullish"

        def verdict(self):
            return "测试"

    monkeypatch.setattr(sig, "analyze", lambda *a, **k: FakeRep())
    r = asyncio.run(md.confluence_signal("TESTUSDT", ("1h", "4h"), {}))
    print("PER:", r["per"])
    assert r["agree"] is True
    assert r["verdict"] == "多周期共振看涨"
    assert r["net"] == int(4 * 1.5)  # (2+2)*1.5


def test_confluence_disagreement_no_boost(monkeypatch):
    """两周期方向分歧：不加成，agree=False，net 为原始和。"""
    import agent.signals as sig

    md = _md_with_klines({"1h": _fake_klines([100] * 120), "4h": _fake_klines([100] * 120)})

    class FakeBull:
        score = 2
        macd_state = "bullish"
        rsi_state = "neutral"
        boll_state = "neutral"
        ma_state = "bullish"

        def verdict(self):
            return "涨"

    class FakeBear:
        score = -2
        macd_state = "bearish"
        rsi_state = "neutral"
        boll_state = "neutral"
        ma_state = "bearish"

        def verdict(self):
            return "跌"

    seq = {"1h": FakeBull(), "4h": FakeBear()}
    monkeypatch.setattr(sig, "analyze", lambda *a, **k: seq[k[2]])  # k[2]=interval
    r = asyncio.run(md.confluence_signal("TESTUSDT", ("1h", "4h"), {}))
    assert r["agree"] is False
    assert r["net"] == 0          # 2 + (-2)
    assert r["verdict"] == "中性"


def test_confluence_bad_interval_handled(monkeypatch):
    """某周期数据不足：不抛异常，该周期按 0 分处理。"""
    md = _md_with_klines({"1h": _fake_klines([100] * 120), "4h": _fake_klines([100] * 10)})
    r = asyncio.run(md.confluence_signal("TESTUSDT", ("1h", "4h"), {}))
    assert r["per"]["4h"]["score"] == 0
    assert "数据不足" in r["per"]["4h"]["verdict"]


def test_watch_add_del_persist(tmp_path_factory=None):
    """自选列表增删查 + 持久化（用工作区显式文件）。"""
    from pathlib import Path

    agent = BinanceAgent.__new__(BinanceAgent)
    wpath = Path(__file__).resolve().parent.parent / ".tmp" / "watch_test.json"
    wpath.parent.mkdir(parents=True, exist_ok=True)
    agent._watch_path = lambda: wpath
    try:
        r1 = asyncio.run(agent.tool_watch("add", "SOLUSDT"))
        assert "已添加 SOLUSDT" in r1
        data = json.loads(wpath.read_text(encoding="utf-8"))
        assert "SOLUSDT" in data

        r2 = asyncio.run(agent.tool_watch("del", "SOLUSDT"))
        assert "已移除" in r2
        data = json.loads(wpath.read_text(encoding="utf-8"))
        assert "SOLUSDT" not in data

        r3 = asyncio.run(agent.tool_watch("del", "SOLUSDT"))
        assert "不在自选列表" in r3
    finally:
        wpath.unlink(missing_ok=True)


def test_watch_add_duplicate_noop():
    from pathlib import Path

    agent = BinanceAgent.__new__(BinanceAgent)
    wpath = Path(__file__).resolve().parent.parent / ".tmp" / "watch_test2.json"
    wpath.parent.mkdir(parents=True, exist_ok=True)
    agent._watch_path = lambda: wpath
    try:
        asyncio.run(agent.tool_watch("add", "BTCUSDT"))
        r = asyncio.run(agent.tool_watch("add", "BTCUSDT"))
        assert "已在自选列表" in r
    finally:
        wpath.unlink(missing_ok=True)
