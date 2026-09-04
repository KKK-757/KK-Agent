"""MCP 连接测试（真实端点联调）。

跑法：python -m pytest tests/test_mcp_client.py -q
注意：涉及真实网络的用例标记 skip（沙盒内无法出网），本地有网时去掉标记可跑。
"""
import json

import pytest

from agent.mcp_client import BinanceMcpClient, MCPError, OFFICIAL_MCP_URL


def test_reject_non_official_endpoint():
    """防钓鱼硬约束：非官方端点直接拒绝。"""
    with pytest.raises(MCPError):
        BinanceMcpClient(url="https://evil.example.com/mcp")


def test_official_endpoint_accepted():
    c = BinanceMcpClient()
    assert c.url == OFFICIAL_MCP_URL
    c.close()


def test_sse_parse_extracts_result():
    """SSE 响应解析：从 event-stream 文本中提取 JSON-RPC result。"""
    sse = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"t1"}]}}\n\n'
    )
    r = BinanceMcpClient._parse_sse(sse)
    assert r["tools"][0]["name"] == "t1"


def test_sse_parse_raises_on_error():
    sse = 'data: {"jsonrpc":"2.0","id":2,"error":{"code":-32000,"message":"boom"}}\n\n'
    with pytest.raises(MCPError):
        BinanceMcpClient._parse_sse(sse)


@pytest.mark.skip(reason="真实端点联调：本地有网环境删除本行运行")
def test_live_connect_and_list_tools():
    """真实联调：握手 + 工具清单（可能触发 OAuth，需要浏览器交互）。"""
    c = BinanceMcpClient()
    try:
        info = c.connect()
        assert info is not None
        tools = c.list_tools()
        names = [t.name for t in tools]
        print("工具清单:", names)
        assert len(names) > 0
    finally:
        c.close()
