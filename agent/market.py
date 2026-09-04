"""Binance market data layer (public API, no Key required).

Only uses public endpoints (prices, klines, depth, 24h stats); read-only and risk-free.
Handles timeouts, retries, and "domain unreachable in some regions" fallbacks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import AgentSettings

log = logging.getLogger("agent.market")

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class MarketError(RuntimeError):
    pass


# 稳定币基础资产：与 USDT 组成的交易对没有交易意义，从榜单中剔除
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USD1", "EUR", "AEUR", "XUSD"}


class MarketData:
    def __init__(self, settings: AgentSettings):
        self.base = settings.binance.api_base.rstrip("/")
        # Public-data fallback domains; the first one is unavailable, so we automatically try the next
        self.fallbacks = [
            "https://data-api.binance.vision",
            "https://api1.binance.com",
            "https://api-gcp.binance.com",
        ]
        self._session: Optional[httpx.AsyncClient] = None
        self._bases_cache: Optional[set] = None
        self._bases_ts: float = 0.0
        self._rank_cache: Dict[str, tuple] = {}

    async def _client(self) -> httpx.AsyncClient:
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(timeout=TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.is_closed:
            await self._session.aclose()

    # ------------------------------------------------------------------
    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET a public endpoint; on failure automatically switch to fallback domains."""
        bases = [self.base] + [b for b in self.fallbacks if b != self.base]
        last_err: Optional[Exception] = None
        for base in bases:
            url = f"{base}{path}"
            try:
                cli = await self._client()
                r = await cli.get(url, params=params)
                r.raise_for_status()
                if base != self.base:
                    log.info("Public data switched to backup domain %s", base)
                return r.json()
            except Exception as e:  # network/HTTP error, try the next domain
                last_err = e
                log.debug("GET %s failed: %s", url, e)
                await asyncio.sleep(0.3)
        raise MarketError(f"公开行情接口不可达（已尝试 {len(bases)} 个域名）：{last_err}")

    # ------------------------------------------------------------------
    async def ticker_price(self, symbol: str) -> Dict[str, Any]:
        """Latest price for a single symbol."""
        return await self._get("/api/v3/ticker/price", {"symbol": symbol.upper()})

    async def ticker_24h(self, symbol: str) -> Dict[str, Any]:
        """24h statistics (price change, high/low, volume, etc.)."""
        return await self._get("/api/v3/ticker/24hr", {"symbol": symbol.upper()})

    async def exchange_info(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params = {"symbol": symbol.upper()} if symbol else None
        return await self._get("/api/v3/exchangeInfo", params)

    async def get_valid_bases(self) -> Optional[set]:
        """返回所有以 USDT 计价的现货基础币种集合（如 BTC/ETH/ZEC）。

        结果缓存 10 分钟；接口不可达时返回 None（调用方回退到内置常用列表）。
        """
        now = time.time()
        if self._bases_cache and now - self._bases_ts < 600:
            return self._bases_cache
        try:
            info = await self.exchange_info()
            bases = {
                s["baseAsset"]
                for s in info.get("symbols", [])
                if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
            }
            if bases:
                self._bases_cache, self._bases_ts = bases, now
            return bases or None
        except Exception as e:
            log.debug("exchangeInfo 拉取失败，使用内置币种列表: %s", e)
            return None

    async def klines(self, symbol: str, interval: str = "1h", limit: int = 300) -> List[List[Any]]:
        """Klines: [openTime, open, high, low, close, volume, ...]."""
        data = await self._get(
            "/api/v3/klines",
            {"symbol": symbol.upper(), "interval": interval, "limit": min(int(limit), 1000)},
        )
        return data

    async def depth(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        return await self._get("/api/v3/depth", {"symbol": symbol.upper(), "limit": min(int(limit), 100)})

    async def _rows_24h(self, quote: str = "USDT", min_volume: float = 1_000_000) -> List[Dict[str, Any]]:
        """全量 24h 统计（稳定币计价现货），缓存 60s，供热度/振幅/涨跌榜共用。"""
        now = time.time()
        cache_key = f"rows:{quote}"
        cached = self._rank_cache.get(cache_key)
        if cached and now - cached[0] < 60:
            return cached[1]
        all_tickers: List[Dict[str, Any]] = await self._get("/api/v3/ticker/24hr")
        rows = []
        for t in all_tickers:
            sym = t.get("symbol", "")
            if not sym.endswith(quote):
                continue
            base = sym[: -len(quote)]
            if not base.isalnum() or any(x in base for x in ("UP", "DOWN", "BULL", "BEAR")):
                continue
            if base in STABLE_BASES:
                continue
            try:
                last = float(t.get("lastPrice", "0"))
                pct = float(t.get("priceChangePercent", "0"))
                qv = float(t.get("quoteVolume", "0"))
                high = float(t.get("highPrice", "0"))
                low = float(t.get("lowPrice", "0"))
            except (TypeError, ValueError):
                continue
            if last <= 0 or qv < min_volume or low <= 0:
                continue
            rows.append({
                "symbol": sym,
                "base": base,
                "price": last,
                "change_pct": pct,
                "quote_volume": qv,
                "amplitude_pct": (high - low) / low * 100,
            })
        self._rank_cache[cache_key] = (now, rows)
        return rows

    async def top_movers(self, quote: str = "USDT", top_n: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        """24h 涨跌幅榜（按价格变化百分比）。"""
        rows = await self._rows_24h(quote)
        rows = sorted(rows, key=lambda x: x["change_pct"], reverse=True)
        return {"gainers": rows[:top_n], "losers": list(reversed(rows[-top_n:]))}

    async def top_volume(self, quote: str = "USDT", top_n: int = 15) -> List[Dict[str, Any]]:
        """24h 成交额榜（市场热度/资金聚集）。"""
        rows = await self._rows_24h(quote)
        return sorted(rows, key=lambda x: x["quote_volume"], reverse=True)[:top_n]

    async def top_amplitude(self, quote: str = "USDT", top_n: int = 15) -> List[Dict[str, Any]]:
        """24h 振幅榜（波动最剧烈）。"""
        rows = await self._rows_24h(quote)
        return sorted(rows, key=lambda x: x["amplitude_pct"], reverse=True)[:top_n]

    async def scan_signals(
        self, quote: str = "USDT", universe: int = 25, interval: str = "1h", params: Optional[Dict] = None
    ) -> List[Dict[str, Any]]:
        """信号扫描：对成交额前 universe 个币批量跑技术指标，返回按得分排序的结果。

        并发 5 路，单币失败自动跳过。
        """
        from .signals import analyze

        rows = await self._rows_24h(quote)
        picks = sorted(rows, key=lambda x: x["quote_volume"], reverse=True)[:universe]
        sem = asyncio.Semaphore(5)

        async def one(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            async with sem:
                try:
                    kl = await self.klines(r["symbol"], interval, 120)
                    rep = analyze(r["symbol"], self.klines_to_closes(kl), interval, params or {})
                    return {
                        "symbol": r["symbol"],
                        "price": r["price"],
                        "change_pct": r["change_pct"],
                        "score": rep.score,
                        "verdict": rep.verdict(),
                        "ma_state": rep.ma_state,
                        "rsi_state": rep.rsi_state,
                        "macd_state": rep.macd_state,
                        "boll_state": rep.boll_state,
                    }
                except Exception as e:
                    log.debug("scan %s 失败: %s", r["symbol"], e)
                    return None

        results = await asyncio.gather(*(one(r) for r in picks))
        out = [x for x in results if x]
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    async def confluence_signal(
        self, symbol: str, intervals: tuple = ("1h", "4h"), params: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """多周期共振分析：两个周期同向才算强信号。

        返回 {symbol, per:{interval:score}, net, verdict, agree}。
        agree=True 表示两周期方向一致（同涨或同跌），此时 |net| 加成。
        """
        from . import signals as _signals

        per: Dict[str, Any] = {}
        for iv in intervals:
            try:
                kl = await self.klines(symbol, iv, 120)
                rep = _signals.analyze(symbol, self.klines_to_closes(kl), iv, params or {})
                per[iv] = {"score": rep.score, "verdict": rep.verdict(), "macd": rep.macd_state, "rsi": rep.rsi_state}
            except Exception as e:
                per[iv] = {"score": 0, "verdict": f"数据不足({e})", "macd": "neutral", "rsi": "neutral"}
        scores = [v["score"] for v in per.values()]
        net = sum(scores)
        agree = all(s > 0 for s in scores) or all(s < 0 for s in scores)
        if agree:
            # 共振加成：两周期同向时净分放大
            net = int(net * 1.5)
        if agree and net > 0:
            verdict = "多周期共振看涨"
        elif agree and net < 0:
            verdict = "多周期共振看跌"
        elif net > 0:
            verdict = "周期分歧偏多"
        elif net < 0:
            verdict = "周期分歧偏空"
        else:
            verdict = "中性"
        return {"symbol": symbol, "per": per, "net": net, "verdict": verdict, "agree": agree}

    # ------------------------------------------------------------------
    @staticmethod
    def klines_to_closes(klines: List[List[Any]]) -> List[float]:
        return [float(k[4]) for k in klines]

    @staticmethod
    def fmt_ticker(t: Dict[str, Any]) -> str:
        return f"{t.get('symbol','?')} = {t.get('price','?')}"


async def selftest(settings: AgentSettings) -> str:
    """连通性自检：拉一次 BTC 价格。"""
    md = MarketData(settings)
    try:
        t = await md.ticker_price("BTCUSDT")
        return f"行情连接正常：BTCUSDT = {t.get('price')}"
    finally:
        await md.close()
