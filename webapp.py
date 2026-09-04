"""Web application (FastAPI): chat + market dashboard.

Trading hard-disabled on the Web side (source=web requests are always rejected by risk control) — for the demo,
it shows the full analysis and simulation path without any fund risk.
Run: python webapp.py  →  http://127.0.0.1:8787
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agent.config import load_settings
from agent.core import BinanceAgent

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web" / "index.html"

settings = load_settings()
agent = BinanceAgent(settings)

app = FastAPI(title="KK-Agent", version="1.0.0")

# ----------------------------------------------------------------------
# 安全加固
# ----------------------------------------------------------------------
import secrets as _secrets
import hmac as _hmac

# 一次性会话令牌：服务启动时生成，仅存于进程内存。
# 浏览器端通过 /api/auth 获取，写接口必须携带（前端自动带上，用户无感）。
AUTH_TOKEN = _secrets.token_urlsafe(24)


@app.middleware("http")
async def security_guard(request, call_next):
    """写接口三重防护：Host 校验（防 DNS rebinding）+ 同源校验（防 CSRF）+ 令牌校验。"""
    host = request.headers.get("host", "")
    write_methods = {"POST", "PUT", "DELETE"}
    is_write = request.method in write_methods
    path = request.url.path

    if is_write:
        # 1) Host 必须是本机形式（防 DNS rebinding 把外部域名指到本服务）
        if host.split(":")[0] not in ("127.0.0.1", "localhost"):
            return JSONResponse({"detail": "只允许本机访问"}, status_code=403)
        # 2) 来源校验：浏览器请求需带本机 Origin/Referer（防跨站伪造）
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        if origin or referer:
            src = origin or referer
            if "127.0.0.1" not in src and "localhost" not in src:
                return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
        # 3) 敏感写操作（Key/设置保存）需会话令牌
        if path.startswith("/api/keys") and request.method == "POST":
            token = request.headers.get("x-bn-token", "")
            if not _hmac.compare_digest(token, AUTH_TOKEN):
                return JSONResponse({"detail": "缺少有效会话令牌"}, status_code=403)

    return await call_next(request)


@app.get("/api/auth")
async def auth_token():
    """发放会话令牌（配合 Host 校验，外部域名拿不到）。"""
    return {"token": AUTH_TOKEN}


class ChatIn(BaseModel):
    message: str


class KeysIn(BaseModel):
    binance_api_key: str = ""
    binance_api_secret: str = ""
    trade_base: str = ""            # testnet / mainnet
    trading_enabled: bool = False
    max_order_notional: float = 100
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""


def _mask(v: str) -> str:
    if not v:
        return ""
    return v[:4] + "*" * (max(len(v) - 8, 2)) + v[-4:] if len(v) > 12 else "****"


@app.get("/api/keys")
async def get_keys():
    """返回当前 Key 掩码与开关状态（绝不回传完整 Key）。"""
    b, l, r = settings.binance, settings.llm, settings.risk
    return {
        "binance_key_masked": _mask(b.api_key),
        "has_binance_key": b.keys_present,
        "trade_base": b.trade_base,
        "is_testnet": b.is_testnet,
        "trading_enabled": r.trading_enabled,
        "max_order_notional": r.max_order_notional,
        "llm_provider": l.provider,
        "llm_key_masked": _mask(l.api_key),
        "has_llm_key": bool(l.api_key) and l.provider != "mock",
    }


@app.post("/api/keys/verify")
async def verify_keys(body: KeysIn):
    """验证用户输入的币安 Key（不落盘），用于保存前确认。"""
    from agent.trading import verify_api_keys

    base = ("https://api.binance.com" if body.trade_base == "mainnet"
            else "https://testnet.binance.vision")
    if not body.binance_api_key:
        # 未输入新 Key 时验证已存的
        if settings.binance.keys_present:
            ok, msg = await verify_api_keys(settings.binance.api_key, settings.binance.api_secret, base)
            return {"ok": ok, "message": msg}
        return {"ok": False, "message": "请先填写 API Key/Secret"}
    ok, msg = await verify_api_keys(body.binance_api_key, body.binance_api_secret, base)
    return {"ok": ok, "message": msg}


@app.post("/api/keys")
async def save_keys(body: KeysIn):
    """保存设置：写入 .env（持久化）+ 热更新内存（立即生效），Key 只存本地文件。"""
    from agent.config import apply_runtime_overrides

    ov: Dict[str, str] = {}
    if body.binance_api_key:
        ov["BIN_AGENT_BINANCE_API_KEY"] = body.binance_api_key
    if body.binance_api_secret:
        ov["BIN_AGENT_BINANCE_API_SECRET"] = body.binance_api_secret
    if body.trade_base:
        ov["BIN_AGENT_BINANCE_TRADE_BASE"] = (
            "https://api.binance.com" if body.trade_base == "mainnet"
            else "https://testnet.binance.vision"
        )
    ov["BIN_AGENT_TRADING_ENABLED"] = "true" if body.trading_enabled else "false"
    if body.max_order_notional and body.max_order_notional > 0:
        ov["BIN_AGENT_MAX_ORDER_NOTIONAL"] = str(body.max_order_notional)
    if body.llm_provider:
        ov["BIN_AGENT_LLM_PROVIDER"] = body.llm_provider
    if body.llm_api_key:
        ov["BIN_AGENT_LLM_API_KEY"] = body.llm_api_key
    if body.llm_base_url:
        ov["BIN_AGENT_LLM_BASE_URL"] = body.llm_base_url
    if body.llm_model:
        ov["BIN_AGENT_LLM_MODEL"] = body.llm_model

    if not ov:
        return {"ok": True, "message": "没有需要保存的变更", "applied": {}}

    applied = apply_runtime_overrides(ov)

    # 热绑定运行中的 agent 单例
    agent.s = settings
    agent.risk.s = settings
    agent.trader.s = settings
    agent.llm.s = settings.llm
    agent.trader.base = settings.binance.trade_base.rstrip("/")

    return {
        "ok": True,
        "message": "已保存并立即生效（写入 .env，重启后依然有效）",
        "applied": {k: _mask(v) for k, v in applied.items()},
        "risk": agent.tool_risk(),
    }


@app.get("/")
async def index():
    if STATIC.exists():
        return FileResponse(STATIC)
    raise HTTPException(404, "web/index.html missing")


@app.get("/api/status")
async def status():
    llm_ready = agent.llm.available and settings.llm.provider != "mock"
    return {
        "agent": settings.agent_name,
        "llm": settings.llm.provider + ("" if llm_ready else " (no key: rule-engine mode)"),
        "risk": agent.tool_risk(),
        "web_trading": "已禁用（安全硬编码）",
        "mcp_endpoint": settings.mcp_endpoint,
    }


@app.get("/api/price/{symbol}")
async def price(symbol: str):
    return JSONResponse({"reply": await agent.tool_price(symbol.upper())})


@app.get("/api/signal/{symbol}")
async def signal(symbol: str, interval: str = "1h"):
    return JSONResponse({"reply": await agent.tool_signal(symbol.upper(), interval)})


@app.get("/api/orders")
async def orders(symbol: str = ""):
    """当前挂单（需要 Key）。"""
    return JSONResponse({"reply": await agent.tool_orders(symbol.upper() if symbol else None)})


@app.get("/api/kline/{symbol}")
async def kline(symbol: str, interval: str = "1h", limit: int = 48):
    """K线数据（供前端画图）：返回归一化后的 OHLC 数组。"""
    kl = await agent.market.klines(symbol.upper(), interval, min(int(limit), 200))
    rows = [
        {
            "t": int(k[0]),
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "v": float(k[5]),
        }
        for k in kl
    ]
    return JSONResponse({"symbol": symbol.upper(), "interval": interval, "candles": rows})


@app.get("/api/confluence/{symbol}")
async def confluence(symbol: str):
    """多周期共振分析（1h+4h）。"""
    return JSONResponse(await agent.market.confluence_signal(symbol.upper(), ("1h", "4h"), settings.signals))


@app.get("/api/backtest/{symbol}")
async def backtest(symbol: str, interval: str = "1h"):
    """历史回测：信号门限策略 vs 买入持有。"""
    try:
        from agent.backtest import run_backtest

        kl = await agent.market.klines(symbol.upper(), interval, 1000)
        r = run_backtest(symbol.upper(), kl, interval, 3, 0, 0.1, settings.signals)
        return JSONResponse({
            "symbol": r.symbol, "interval": r.interval, "days": r.days,
            "strategy_pct": round(r.strategy_pct, 2), "buy_hold_pct": round(r.buy_hold_pct, 2),
            "outperformance": round(r.outperformance, 2), "trades": r.trades,
            "win_rate": round(r.win_rate, 0), "max_drawdown_pct": round(r.max_drawdown_pct, 2),
            "equity": [round(x, 4) for x in r.equity[:: max(1, len(r.equity) // 120)]],
            "bench": [round(x, 4) for x in r.bench[:: max(1, len(r.bench) // 120)]],
        })
    except (ValueError, Exception) as e:  # noqa: BLE001
        raise HTTPException(500, str(e))


class WatchIn(BaseModel):
    action: str = "list"        # list / add / del
    symbol: str = ""


@app.post("/api/watch")
async def watch(body: WatchIn):
    return JSONResponse({"reply": await agent.tool_watch(body.action, body.symbol.upper())})


@app.get("/api/coins")
async def coins():
    """全部可用币种（来自币安真实 USDT 交易对，缓存 10 分钟）。"""
    bases = await agent.market.get_valid_bases()
    return JSONResponse({"coins": sorted(bases) if bases else []})


@app.get("/api/rank")
async def rank(tab: str = "movers"):
    """榜单：movers 涨跌 / volume 热度 / amplitude 振幅。"""
    top_n = int(settings.market.get("top_n", 10))
    quote = settings.market.get("quote_asset", "USDT")
    if tab == "volume":
        return JSONResponse({"rows": await agent.market.top_volume(quote, 15)})
    if tab == "amplitude":
        return JSONResponse({"rows": await agent.market.top_amplitude(quote, 15)})
    return JSONResponse(await agent.market.top_movers(quote, top_n))


@app.get("/api/scan")
async def scan(interval: str = "1h"):
    """AI 信号扫描：成交额前 25 币批量技术指标。"""
    quote = settings.market.get("quote_asset", "USDT")
    rows = await agent.market.scan_signals(quote, 25, interval, settings.signals)
    return JSONResponse({"rows": rows, "interval": interval})


@app.get("/api/movers")
async def movers():
    top_n = int(settings.market.get("top_n", 10))
    quote = settings.market.get("quote_asset", "USDT")
    data = await agent.market.top_movers(quote, top_n)
    return JSONResponse(data)


@app.get("/api/report/{symbol}")
async def report(symbol: str):
    return JSONResponse({"reply": await agent.tool_report(symbol.upper())})


@app.post("/api/chat")
async def chat(body: ChatIn):
    try:
        reply = await agent.chat(body.message)
        return {"reply": reply}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


@app.on_event("shutdown")
async def _shutdown():
    await agent.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.web.host, port=settings.web.port)
