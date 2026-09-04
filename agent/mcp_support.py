"""Binance MCP integration support (preparation for Track 2).

Binance's official MCP is a "remote MCP": no local process is needed; just add the official endpoint URL
to an MCP-compatible client (Claude Desktop / Cursor / Claude Code, etc.) and complete the authorization flow.

This module is responsible for:
1. Generating ready-to-paste config snippets for each client;
2. Verifying that the endpoint the user fills in is the official address (anti-phishing);
3. Providing the action checklist for Track 2 tasks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

OFFICIAL_ENDPOINT = "https://agent.binance.com/mcp/agentic"
OFFICIAL_DOCS = "https://developers.binance.com/en/docs/agent-native/mcp-server"
OFFICIAL_FAQ = "https://www.binance.com/zh-CN/support/faq/detail/7a6e676e36fb455d96478932cb12d9f3"

TRACK2_TASKS = [
    "现货交易：通过 MCP 完成一笔现货买卖（市价/限价均可）",
    "合约交易：完成一笔合约交易（注意杠杆，先小额）",
    "杠杆/闪兑：完成一笔杠杆交易或闪兑操作",
]


@dataclass
class McpCheck:
    ok: bool
    message: str


def verify_endpoint(url: str) -> McpCheck:
    """校验用户提供的 MCP 端点是否为官方地址。"""
    u = (url or "").strip().rstrip("/")
    if u == OFFICIAL_ENDPOINT:
        return McpCheck(True, "官方端点，校验通过")
    if "binance.com" in u:
        return McpCheck(False, f"这是 binance.com 地址但不是官方 MCP 端点，官方应为：{OFFICIAL_ENDPOINT}")
    return McpCheck(
        False,
        "不是官方端点！接入可能泄露账户权限（钓鱼风险）。官方文档：" + OFFICIAL_DOCS,
    )


def config_for_claude_desktop(endpoint: str = OFFICIAL_ENDPOINT) -> str:
    """The snippet to paste into claude_desktop_config.json."""
    cfg = {
        "mcpServers": {
            "binance": {
                "url": endpoint,
            }
        }
    }
    return json.dumps(cfg, indent=2, ensure_ascii=False)


def config_for_cursor(endpoint: str = OFFICIAL_ENDPOINT) -> str:
    """Cursor: MCP configuration (in Settings > MCP)."""
    cfg = {"mcpServers": {"binance": {"url": endpoint}}}
    return json.dumps(cfg, indent=2, ensure_ascii=False)


def claude_code_command(endpoint: str = OFFICIAL_ENDPOINT) -> str:
    """Claude Code one-liner command (HTTP remote MCP uses the --transport http form)."""
    return f'claude mcp add binance --transport http {endpoint}'


def full_guide() -> str:
    chk = verify_endpoint(OFFICIAL_ENDPOINT)
    return (
        "=== 币安官方 MCP 接入指引（赛道二）===\n"
        f"1. 官方端点（仅此一个，逐字核对）：{OFFICIAL_ENDPOINT}\n"
        f"2. 校验方式：运行 `python -m cli mcp` 或人工核对：{chk.message}\n"
        "3. 按你的客户端选择其一：\n"
        "   - Claude Code（终端命令）：\n"
        f"       {claude_code_command()}\n"
        "   - Claude Desktop（claude_desktop_config.json）：\n"
        f"{config_for_claude_desktop()}\n"
        "   - Cursor（设置 -> MCP -> 添加自定义 MCP）：\n"
        f"{config_for_cursor()}\n"
        "4. 连接后完成授权流程（账户绑定 + 交易限额设置；建议子账户 + 低限额）\n"
        "5. 让 Agent 完成以下任一操作即算完成任务：\n"
        f"    - " + "\n    - ".join(TRACK2_TASKS) + "\n"
        f"6. 奖励：前 10,000 名完成者各得 4 USDC | 官方 FAQ：{OFFICIAL_FAQ}\n"
        "安全红线：不要在任何第三方页面输入账户密码；核对域名 binance.com；"
        "API/MCP 权限绝不包含提币。"
    )
