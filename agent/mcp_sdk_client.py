"""基于官方 MCP Python SDK 的币安 MCP 连接器（替代手写协议实现）。

SDK（pip install mcp）内置了完整规范流程：
  - Streamable HTTP 传输 + JSON-RPC
  - OAuth 2.1 授权框架（401 → protected resource metadata → 动态客户端注册 →
    授权端点跳转 → PKCE → 令牌交换），含令牌持久化

本模块只做三件事：发起连接、列工具、调工具。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent.mcp_client import MCPError, OFFICIAL_MCP_URL

log = logging.getLogger("agent.mcp_sdk")


class SdkMcpClient:
    """官方 SDK 的 streamable-http 客户端封装（异步）。"""

    def __init__(self, url: str = OFFICIAL_MCP_URL):
        if url.rstrip("/") != OFFICIAL_MCP_URL:
            raise MCPError(f"拒绝非官方 MCP 端点：{url}")
        self.url = OFFICIAL_MCP_URL
        self._session = None
        self._streams_cm = None
        # 代理：agent.binance.com 在部分网络需要代理访问
        self.proxy = os.getenv("BIN_AGENT_MCP_PROXY") or os.getenv("HTTPS_PROXY") or None

    async def connect(self) -> Dict[str, Any]:
        """建立连接（含 OAuth 2.1 自动授权），返回服务器信息。"""
        import httpx2
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        import webbrowser
        import secrets as _secrets

        http_client = httpx2.AsyncClient(proxy=self.proxy, timeout=30)

        # --- OAuth 2.1 客户端（SDK 规范流程：401→元数据→注册→授权→令牌） ---
        # 回调端口固定 18731（币安 MCP 授权时可接受；首次运行自动注册 client）
        callback_port = 18731
        redirect_uri = f"http://127.0.0.1:{callback_port}/callback"

        metadata = OAuthClientMetadata(
            client_name="KK-Agent",
            client_uri="https://github.com/KKK-757/KK-Agent",
            redirect_uris=[redirect_uri],
            token_endpoint_auth_method="none",       # 公共客户端（PKCE）
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

        class _ConsoleTokenStorage:
            """内存令牌 + 客户端注册信息存储（SDK 要求的完整接口）。"""
            def __init__(self):
                self._tokens = None
                self._client_info = None
            async def get_tokens(self):
                return self._tokens
            async def set_tokens(self, tokens):
                self._tokens = tokens
            async def get_client_info(self):
                return self._client_info
            async def set_client_info(self, info):
                self._client_info = info

        auth_provider = OAuthClientProvider(
            server_url=self.url,
            client_metadata=metadata,
            storage=_ConsoleTokenStorage(),
            redirect_handler=lambda url: webbrowser.open(url),
        )

        async def _callback_handler():
            """本地起 HTTP 回调服务器接收授权码。"""
            import threading
            from http.server import BaseHTTPRequestHandler, HTTPServer
            result: Dict[str, str] = {}
            done = asyncio.Event()

            class H(BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    for k, v in qs.items():
                        result[k] = v[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    ok = "code" in result
                    self.wfile.write(
                        ("<h2>授权成功，回到终端继续</h2>" if ok else "<h2>授权失败</h2>").encode()
                    )
                    threading.Thread(target=lambda: (done.set() and None)).start()
                def log_message(self, *a):
                    pass

            server = HTTPServer(("127.0.0.1", callback_port), H)
            server.timeout = 300
            loop = asyncio.get_running_loop()
            while "code" not in result and "error" not in result:
                await loop.run_in_executor(None, server.handle_request)
            server.server_close()
            if "error" in result:
                raise MCPError(f"授权被拒绝：{result['error']}")
            return {"authorization_code": result["code"], "code_verifier": _pkce_verifier, "state": result.get("state", "")}

        # PKCE verifier 需在授权 URL 构造前生成——SDK 内部处理，回调只交回 code
        auth_provider = OAuthClientProvider(
            server_url=self.url,
            client_metadata=metadata,
            storage=_ConsoleTokenStorage(),
            redirect_handler=lambda url: webbrowser.open(url),
            callback_handler=_callback_handler,
        )

        http_client = httpx2.AsyncClient(
            proxy=self.proxy, timeout=30, auth=auth_provider
        )
        self._streams_cm = streamable_http_client(self.url, http_client=http_client)
        # 本 SDK 版本 yield (read, write) 二元组
        read, write = await self._streams_cm.__aenter__()

        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        info = await self._session.initialize()
        return {
            "server_name": info.serverInfo.name if info.serverInfo else "?",
            "server_version": info.serverInfo.version if info.serverInfo else "?",
            "protocol_version": info.protocolVersion,
            "instructions": (info.instructions or "")[:200],
        }

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "schema": (t.inputSchema or {})}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        result = await self._session.call_tool(name, arguments or {})
        texts = [c.text for c in (result.content or []) if getattr(c, "type", "") == "text"]
        raw = "\n".join(texts)
        import json as _json
        try:
            return _json.loads(raw) if raw else {"isError": result.isError}
        except _json.JSONDecodeError:
            return raw or {"isError": result.isError}

    async def close(self) -> None:
        for cm in (self._session, self._streams_cm):
            if cm:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        self._session = None
        self._streams_cm = None


def run_with_sdk(connect_only: bool = False, tool: Optional[str] = None, args: Optional[Dict] = None) -> None:
    """CLI 同步入口：连接 → （可选）列工具 / 调工具。"""

    async def _run() -> None:
        c = SdkMcpClient()
        try:
            info = await c.connect()
            print(f"[MCP] 已连接：{info.get('server_name')} v{info.get('server_version')} "
                  f"(协议 {info.get('protocol_version')})")
            if info.get("instructions"):
                print(f"[MCP] 说明: {info['instructions']}")
            if connect_only or tool is None:
                tools = await c.list_tools()
                print(f"\n[MCP] 可用工具 {len(tools)} 个：")
                for t in tools:
                    print(f"  - {t['name']}: {t['description'][:90]}")
            else:
                result = await c.call_tool(tool, args or {})
                import json as _json
                print(_json.dumps(result, ensure_ascii=False, indent=1)[:3000])
        finally:
            await c.close()

    asyncio.run(_run())
