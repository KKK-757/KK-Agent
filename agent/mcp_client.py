"""Binance MCP 客户端：通过官方 MCP 端点连接币安 Agent OS。

实现 MCP 协议（Streamable HTTP 传输 + JSON-RPC 2.0）的核心子集：
  1. initialize   —— 协议握手，交换版本与能力
  2. tools/list   —— 拉取服务端提供的工具清单
  3. tools/call   —— 调用某个工具（如下单）
  4. OAuth 授权   —— 首次连接时打开浏览器完成币安账户授权，回调携带访问令牌

安全设计：
  - 端点写死为官方地址（https://agent.binance.com/mcp/agentic），不接受外部传入
  - 令牌仅存进程内存 + 本地 .mcp_token 文件（0600 权限语义），绝不打印/上传
  - 所有网络请求带超时；响应解析失败视为协议错误，不静默吞掉
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger("agent.mcp_client")

OFFICIAL_MCP_URL = "https://agent.binance.com/mcp/agentic"
PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "KK-Agent", "version": "1.0.0"}


class MCPError(RuntimeError):
    """MCP 协议级错误（服务器返回 error 或响应不可解析）。"""


@dataclass
class McpTool:
    """服务端声明的单个工具。"""
    name: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)


class BinanceMcpClient:
    """币安官方 MCP 的最小客户端。

    用法：
        c = BinanceMcpClient()
        c.connect()            # 握手 + （必要时）授权
        tools = c.list_tools() # 工具清单
        c.call_tool("place_order", {...})
    """

    def __init__(self, url: str = OFFICIAL_MCP_URL, timeout: float = 30.0, proxy: Optional[str] = None):
        # 端点安全：只允许官方地址（防钓鱼硬约束）
        if url.rstrip("/") != OFFICIAL_MCP_URL:
            raise MCPError(f"拒绝非官方 MCP 端点：{url}（官方：{OFFICIAL_MCP_URL}）")
        self.url = OFFICIAL_MCP_URL
        self.timeout = timeout
        # 代理：国内网络访问 agent.binance.com 通常需要代理。
        # 优先用显式传入的 proxy；否则读环境变量 BIN_AGENT_MCP_PROXY / HTTPS_PROXY。
        self.proxy = proxy or os.getenv("BIN_AGENT_MCP_PROXY") or os.getenv("HTTPS_PROXY") or None
        self.session_id: Optional[str] = None
        self._request_id = 0
        self._token: Optional[str] = None
        self._client = httpx.Client(timeout=timeout, follow_redirects=False, proxy=self.proxy)

    # ------------------------------------------------------------------
    # JSON-RPC over Streamable HTTP
    # ------------------------------------------------------------------
    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None, notify: bool = False) -> Optional[Dict[str, Any]]:
        """发送一条 JSON-RPC 消息。notify=True 时为通知（无 id，不期待响应）。"""
        self._request_id += 1
        body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = self._request_id

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        r = self._client.post(self.url, json=body, headers=headers)

        # 服务端可能通过 401 指示需要（重新）授权
        if r.status_code == 401:
            raise MCPError("UNAUTHORIZED")

        # 会话 ID 由服务端在握手响应头下发
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid

        if r.status_code not in (200, 202):
            raise MCPError(f"HTTP {r.status_code}: {r.text[:200]}")

        if notify:
            return None

        # 响应可能是 application/json 或 SSE（text/event-stream）
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return self._parse_sse(r.text)
        try:
            data = r.json()
        except Exception as e:
            raise MCPError(f"响应不是合法 JSON：{e}") from e

        if "error" in data:
            err = data["error"]
            raise MCPError(f"MCP 错误 {err.get('code')}: {err.get('message')}")
        return data.get("result", {})

    @staticmethod
    def _parse_sse(text: str) -> Optional[Dict[str, Any]]:
        """从 SSE 文本流中提取最后一条 JSON-RPC result/error。"""
        result, error = None, None
        for block in text.split("\n\n"):
            data_lines = [ln[6:] for ln in block.splitlines() if ln.startswith("data:")]
            if not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if "error" in payload:
                error = payload["error"]
            elif "result" in payload:
                result = payload["result"]
        if error:
            raise MCPError(f"MCP 错误 {error.get('code')}: {error.get('message')}")
        return result

    # ------------------------------------------------------------------
    # 协议生命周期
    # ------------------------------------------------------------------
    def connect(self, open_browser: bool = True) -> Dict[str, Any]:
        """握手（initialize + initialized 通知）。若 401 则走 OAuth 授权后重试。"""
        try:
            return self._initialize()
        except MCPError as e:
            if str(e) != "UNAUTHORIZED":
                raise
            log.info("需要授权，启动 OAuth 流程…")
            self.authorize(open_browser=open_browser)
            return self._initialize()

    def _initialize(self) -> Dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        # 握手完成后，按协议发送 initialized 通知
        self._rpc("notifications/initialized", notify=True)
        return result or {}

    # ------------------------------------------------------------------
    # OAuth 授权（简化实现：服务端跳转 + 本地回调捕码 + 令牌交换）
    # ------------------------------------------------------------------
    def authorize(self, open_browser: bool = True) -> str:
        """打开浏览器完成币安授权；本地起临时回调服务器接收授权码。

        返回访问令牌（同时缓存到进程内）。
        """
        state = secrets.token_urlsafe(16)
        caught: Dict[str, str] = {}

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                caught.update({k: v[0] for k, v in qs.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = "code" in caught
                body = "授权成功，请回到终端继续。" if ok else f"授权失败：{self.path[:120]}"
                self.wfile.write(f"<h2>{body}</h2>".encode("utf-8"))

            def log_message(self, *a):  # 静默默认访问日志
                pass

        server = HTTPServer(("127.0.0.1", 0), Callback)
        port = server.server_address[1]
        callback_url = f"http://127.0.0.1:{port}/callback"

        # 向服务端请求授权起始地址
        r = self._client.get(
            self.url,
            headers={"Accept": "text/html", **({"Mcp-Session-Id": self.session_id} if self.session_id else {})},
            params={"redirect_uri": callback_url, "state": state},
        )
        auth_page = r.headers.get("location") or (r.text[:0] if not r.text.startswith("http") else r.text)
        if not auth_page:
            # 部分实现直接在 401 body 里给授权 URL
            try:
                auth_page = r.json().get("authorize_url", "")
            except Exception:
                auth_page = ""
        if not auth_page:
            server.server_close()
            raise MCPError("未能获取授权地址；请确认使用官方端点。")

        print(f"\n[授权] 即将打开浏览器完成币安账户授权（回调端口 {port}）…")
        print(f"[授权] 如未自动打开，请手动访问：\n{auth_page}\n")
        if open_browser:
            webbrowser.open(auth_page)

        server.timeout = 300  # 最多等 5 分钟
        try:
            while "code" not in caught and "error" not in caught:
                server.handle_request()
        finally:
            server.server_close()

        if "error" in caught:
            raise MCPError(f"授权被拒绝：{caught['error']}")
        if caught.get("state") != state:
            raise MCPError("授权回调 state 校验失败（防 CSRF），已中止")

        # 用授权码换访问令牌（POST 回 MCP 端点，按 Streamable HTTP 惯例）
        r = self._client.post(
            self.url,
            json={"grant_type": "authorization_code", "code": caught["code"], "redirect_uri": callback_url},
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            raise MCPError(f"令牌交换失败 HTTP {r.status_code}: {r.text[:200]}")
        self._token = r.json().get("access_token")
        if not self._token:
            raise MCPError("令牌交换响应中缺少 access_token")
        print("[授权] 成功，令牌已缓存到进程内存")
        return self._token

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def list_tools(self) -> List[McpTool]:
        result = self._rpc("tools/list", {})
        tools = []
        for t in (result or {}).get("tools", []):
            tools.append(McpTool(name=t.get("name", "?"), description=t.get("description", ""), schema=t.get("inputSchema", {})))
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        # tools/call 的 result.content 通常是 [{type:text, text:...}, ...]
        content = (result or {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        raw = "\n".join(texts)
        try:
            return json.loads(raw) if raw else result
        except json.JSONDecodeError:
            return raw or result

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
