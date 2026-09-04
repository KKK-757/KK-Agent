"""CLI entry point.

Usage:
    python -m cli chat              Conversation mode (rule engine or LLM)
    python -m cli price BTCUSDT     Latest price
    python -m cli signal BTCUSDT    Technical signals
    python -m cli movers            Top gainers/losers
    python -m cli report ETHUSDT    Market report
    python -m cli buy 50 USDT of BTC     下单（去向由 .env 的 TRADE_BASE 决定：测试网/正式盘）
    python -m cli balances          Account balance (requires Key)
    python -m cli risk              Risk-control status
    python -m cli mcp               MCP integration guide (Track 2)
    python -m cli capabilities      Capability inventory
    python -m cli demo              Full-capability demo
    python -m cli selftest          Connectivity self-test
"""
from __future__ import annotations

import asyncio
import sys

# Windows console GBK encoding issue: force UTF-8 output, to avoid garbled text when recording demo videos
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from agent.config import load_settings
from agent.core import BinanceAgent, RISK_NOTE
from agent.market import selftest

BANNER = "KK-Agent 0.1.0 (Binance Agent OS)"

HELP = """
命令列表：
  chat                    对话模式（输入 exit 退出）
  price <SYMBOL>          最新价格，例如 price BTCUSDT
  signal <SYMBOL>         技术信号（MA/RSI/MACD/布林带）
  confluence <SYMBOL>     多周期共振分析（1h+4h）
  backtest <SYMBOL>       历史回测：信号策略 vs 买入持有
  movers                  24h 涨跌幅榜
  report <SYMBOL>         市场报告
  watch [add/del <SYM>]   自选列表（查看/添加/移除）
  buy 50 USDT of BTC      下单（去向由 TRADE_BASE 决定）
  buy ... at 60000        限价单示例
  orders [SYMBOL]         当前挂单（需要 API Key）
  cancel <ID>             撤销挂单
  sell 0.01 BTC           按数量卖出（自动折算金额）
  balances                账户余额（需要 API Key）
  risk                    风控状态
  testnet                 模拟盘（Test Network）接入指引
  mcp                     币安官方 MCP 接入指引
  capabilities            能力清单
  selftest                连通性自检
  help                    本帮助
"""


async def _confirm_trade(req) -> bool:
    """CLI 真实下单前的手动确认回调。"""
    print(f"\n[确认] 即将真实下单：{req.action.upper()} {req.symbol} 金额 {req.quote_qty} USDT")
    ans = input("确认执行？(yes/no)：").strip().lower()
    return ans in ("y", "yes")


async def main(argv: list[str]) -> int:
    print(BANNER)
    s = load_settings()
    agent = BinanceAgent(s)
    try:
        if not argv or argv[0] in ("help", "-h", "--help"):
            print(HELP)
            print(agent.llm.describe())
            print(agent.tool_risk())
            return 0

        cmd, args = argv[0].lower(), argv[1:]

        if cmd == "selftest":
            print(await selftest(s))
            return 0

        if cmd == "chat":
            print(agent.llm.describe())
            print(agent.tool_risk())
            print('输入 "exit" 退出。可以试："BTC 价格"、"行情报告"、"涨跌榜"、"buy 20 USDT of ETH"\n')
            while True:
                try:
                    line = input("you > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    continue
                if line.lower() in ("exit", "quit", "q"):
                    break
                print(f"\n{agent.s.agent_name} > {await agent.chat(line)}\n")
            return 0

        if cmd == "price":
            sym = (args[0] if args else "BTCUSDT").upper()
            print(await agent.tool_price(sym))
            return 0

        if cmd == "signal":
            sym = (args[0] if args else "BTCUSDT").upper()
            interval = args[1] if len(args) > 1 else None
            print(await agent.tool_signal(sym, interval))
            return 0

        if cmd == "movers":
            print(await agent.tool_movers())
            return 0

        if cmd == "report":
            sym = (args[0] if args else "BTCUSDT").upper()
            print(await agent.tool_report(sym))
            return 0

        if cmd in ("buy", "sell"):
            text = " ".join(argv)
            # Explicit CLI form: buy <qty> USDT of <SYM> / sell <qty> <COIN>
            print(await agent.tool_trade(text, source="cli", confirm_cb=_confirm_trade))
            print(RISK_NOTE)
            return 0

        if cmd == "balances":
            print(await agent.tool_balances())
            return 0

        if cmd == "risk":
            print(agent.tool_risk())
            print("\n安全设计：提币/划转在代码层没有调用路径（硬约束），不受任何开关影响。")
            return 0

        if cmd == "testnet":
            base = s.binance.trade_base
            on = "模拟盘" if "testnet" in base else "正式盘（真钱）"
            print(f"当前交易基地址：{base}（{on}）")
            print(agent.tool_risk())
            print(
                "\n接入币安模拟盘（Spot Test Network，免费虚拟资金）：\n"
                "  1. 打开 https://testnet.binance.vision ，用 GitHub 账号登录\n"
                "  2. 页面生成测试 API Key/Secret\n"
                "  3. 填入 .env：BIN_AGENT_BINANCE_API_KEY / BIN_AGENT_BINANCE_API_SECRET\n"
                "  4. .env 中设 BIN_AGENT_TRADING_ENABLED=true\n"
                "  5. .env 中 BIN_AGENT_BINANCE_TRADE_BASE=https://testnet.binance.vision（当前已是）\n"
                "  6. 重新运行，即可 buy / sell / balances 真实打到测试网\n"
                "注意：测试网行情与正式盘一致，成交为模拟撮合；余额是虚拟的，赚亏都不影响真实资产。"
            )
            return 0

        if cmd == "mcp":
            print(agent.tool_mcp())
            return 0

        if cmd == "capabilities":
            print(agent.tool_capabilities())
            return 0

        if cmd == "confluence":
            sym = (args[0] if args else "BTCUSDT").upper()
            print(await agent.tool_confluence(sym))
            return 0

        if cmd == "backtest":
            sym = (args[0] if args else "BTCUSDT").upper()
            interval = args[1] if len(args) > 1 else "1h"
            print(await agent.tool_backtest(sym, interval))
            return 0

        if cmd == "watch":
            action = args[0] if args else "list"
            sym = args[1].upper() if len(args) > 1 else ""
            if action in ("add", "del") and sym and not sym.endswith(("USDT", "USDC")):
                sym += "USDT"
            print(await agent.tool_watch(action, sym))
            return 0

        if cmd == "orders":
            sym = args[0].upper() if args else None
            print(await agent.tool_orders(sym))
            return 0

        if cmd == "cancel":
            if not args:
                print("用法：cancel <订单ID> [SYMBOL]")
                return 1
            sym = args[1].upper() if len(args) > 1 else "BTCUSDT"
            print(await agent.tool_cancel(sym, int(args[0])))
            return 0

        print(f"未知命令：{cmd}\n{HELP}")
        return 1
    finally:
        await agent.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
