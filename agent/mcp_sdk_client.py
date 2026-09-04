"""基于官方 MCP Python SDK 的币安 MCP 连接器（网页化版本）。

在 SDK 封装之上增加"网页体验"所需的三块基础设施：
  1. Token 持久化 —— 授权一次，之后重启程序都不再弹授权
  2. 授权流程状态机 —— 网页点「连接」→ 后台生成授权链接并打开浏览器 →
     本地回调服务器收码换 token → 状态推回网页轮询
  3. 连接后的会话保活 —— tools/call 随时可调
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent.mcp_client import MCPError, OFFICIAL_MCP_URL

log = logging.getLogger("agent.mcp_sdk")

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / ".mcp_token"          # OAuth 令牌持久化（本地，不进 git）
CALLBACK_PORT = 18731                      # 授权回调端口（与元数据 redirect_uri 一致）


# ----------------------------------------------------------------------
# Token 持久化存储（SDK 要求的接口 + 落盘）
# ----------------------------------------------------------------------
class _FileTokenStorage:
    """令牌存本地 .mcp_token 文件（JSON），重启程序不丢。"""

    def __init__(self):
        self._tokens = None
        self._client_info = None
        self._load()

    def _load(self) -> None:
        try:
            if TOKEN_FILE.exists():
                data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
                self._tokens = data.get("tokens")
                self._client_info = data.get("client_info")
        except Exception as e:
            log.warning("读取 token 文件失败（忽略，按未授权处理）: %s", e)

    def _save(self) -> None:
        try:
            data = {
                "tokens": self._tokens.model_dump(mode="json") if self._tokens and hasattr(self._tokens, "model_dump") else self._tokens,
                "client_info": self._client_info.model_dump(mode="json") if self._client_info and hasattr(self._client_info, "model_dump") else self._client_info,
            }
            TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.warning("写入 token 文件失败: %s", e)

    async def get_tokens(self):
        return self._tokens

    async def set_tokens(self, tokens):
        self._tokens = tokens
        self._save()

    async def get_client_info(self):
        return self._client_info

    async def set_client_info(self, info):
        self._client_info = info
        self._save()

    @staticmethod
    def clear() -> None:
        try:
            TOKEN_FILE.unlink(missing_ok=True)
        except Exception:
            pass


# ----------------------------------------------------------------------
# 授权状态机（供网页轮询）
# ----------------------------------------------------------------------
@dataclass
class AuthState:
    status: str = "idle"              # idle / starting / waiting_browser / success / failed
    detail: str = ""
    auth_url: str = ""
    updated_at: float = field(default_factory=time.time)


# ----------------------------------------------------------------------
# 客户端
# ----------------------------------------------------------------------
class SdkMcpClient:
    """官方 SDK 的 streamable-http 客户端封装（异步）+ 网页化授权状态机。"""

    def __init__(self, url: str = OFFICIAL_MCP_URL):
        if url.rstrip("/") != OFFICIAL_MCP_URL:
            raise MCPError(f"拒绝非官方 MCP 端点：{url}")
        self.url = OFFICIAL_MCP_URL
        self._session = None
        self._streams_cm = None
        self._http_client = None
        self.proxy = os.getenv("BIN_AGENT_MCP_PROXY") or os.getenv("HTTPS_PROXY") or None
        self.storage = _FileTokenStorage()
        self.auth_state = AuthState()

    # ------------------------------------------------------------------
    # 连接（含 OAuth；token 已持久化时不弹浏览器）
    # ------------------------------------------------------------------
    async def connect(self) -> Dict[str, Any]:
        import httpx2
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata
        import webbrowser

        self.auth_state.status = "starting"
        self.auth_state.detail = "建立连接…"

        metadata = OAuthClientMetadata(
            client_name="KK-Agent",
            client_uri="https://github.com/KKK-757/KK-Agent",
            redirect_uris=[f"http://127.0.0.1:{CALLBACK_PORT}/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

        # 授权链接打开 + 本地回调：转为「后台线程收码 + 状态机轮询」
        def redirect_handler(url: str):
            self.auth_state.status = "waiting_browser"
            self.auth_state.auth_url = url
            self.auth_state.detail = "浏览器已打开，请在网页登录币安并点击授权"
            webbrowser.open(url)

        async def callback_handler():
            code = await self._wait_for_callback()
            return {"code": code, "state": getattr(self, "_callback_state", None)}

        auth_provider = OAuthClientProvider(
            server_url=self.url,
            client_metadata=metadata,
            storage=self.storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            # 关键：币安不支持 RFC7591 动态注册（registration 404），
            # 改用「URL 即 client_id」模式——client_id 就是仓库里的元数据文档地址。
            # 注意：即便如此，币安 Agentic 授权页还有客户端白名单校验（当前仅
            # Claude Desktop / Cursor 等预注册客户端可通过），白名单开放后此处
            # 零改动即可接入。见 README「已知限制」。
            client_metadata_url="https://raw.githubusercontent.com/KKK-757/KK-Agent/main/oauth-client-metadata.json",
        )

        self._http_client = httpx2.AsyncClient(proxy=self.proxy, timeout=30, auth=auth_provider)
        self._streams_cm = streamable_http_client(self.url, http_client=self._http_client)
        read, write = await self._streams_cm.__aenter__()

        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        info = await self._session.initialize()

        self.auth_state.status = "success"
        self.auth_state.detail = f"已连接 {info.serverInfo.name}"
        return {
            "server_name": info.serverInfo.name if info.serverInfo else "?",
            "server_version": info.serverInfo.version if info.serverInfo else "?",
            "protocol_version": info.protocolVersion,
            "instructions": (info.instructions or "")[:200],
        }

    # ------------------------------------------------------------------
    # 授权回调服务器（后台线程，网页流程专用）
    # ------------------------------------------------------------------
    def _start_callback_server_once(self) -> None:
        """起一次性本地回调服务器（后台线程），收到的授权码写入 self._auth_code。"""
        if getattr(self, "_callback_server", None):
            return  # 已在监听

        result: Dict[str, str] = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                qs = parse_qs(urlparse(self.path).query)
                for k, v in qs.items():
                    result[k] = v[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = "code" in result
                body = "授权成功！请回到 KK-Agent 网页查看。" if ok else f"授权失败：{self.path[:120]}"
                self.wfile.write(f"<h2>{body}</h2>".encode("utf-8"))

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), H)
        self._callback_server = server
        self._auth_code_result = result

        def _serve():
            server.timeout = 600
            try:
                while not result.get("code") and not result.get("error"):
                    server.handle_request()
            except Exception:
                pass

        threading.Thread(target=_serve, daemon=True).start()

    async def _wait_for_callback(self) -> str:
        """等待后台线程收到授权码（最多 5 分钟）。"""
        self._start_callback_server_once()
        deadline = time.time() + 300
        while time.time() < deadline:
            r = getattr(self, "_auth_code_result", {})
            if r.get("code"):
                self.auth_state.status = "success"
                self.auth_state.detail = "授权码已收到，正在换取令牌…"
                self._stop_callback_server()
                return r["code"]
            if r.get("error"):
                self._stop_callback_server()
                raise MCPError(f"授权被拒绝：{r['error']}")
            await asyncio.sleep(0.5)
        self._stop_callback_server()
        raise MCPError("等待授权超时（5 分钟）")

    def _stop_callback_server(self) -> None:
        server = getattr(self, "_callback_server", None)
        if server:
            try:
                server.server_close()
            except Exception:
                pass
            self._callback_server = None

    def open_auth_page(self) -> str:
        """网页流程入口：生成 PKCE + 打开浏览器授权页（不阻塞）。"""
        from mcp.shared.auth import OAuthClientMetadata

        # PKCE（授权码交换需要；SDK 的 auth provider 会用 storage 里的 client 信息
        # 自动走标准流程，这里生成 challenge 仅为浏览器 URL 使用）
        verifier = secrets.token_urlsafe(48)
        import hashlib
        import base64
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()

        self._pkce_verifier = verifier
        self.auth_state = AuthState(status="waiting_browser")
        self.auth_state.detail = "浏览器已打开授权页，请登录币安并点击授权"

        metadata = OAuthClientMetadata(
            client_name="KK-Agent",
            client_uri="https://github.com/KKK-757/KK-Agent",
            redirect_uris=[f"http://127.0.0.1:{CALLBACK_PORT}/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        # 构造授权 URL（按 OAuthClientMetadata 序列化的标准参数）
        base = "https://accounts.binance.com/agentic-oauth/authorize"
        params = {
            "response_type": "code",
            "client_id": "KK-Agent",
            "redirect_uri": f"http://127.0.0.1:{CALLBACK_PORT}/callback",
            "state": secrets.token_urlsafe(8),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        from urllib.parse import urlencode
        auth_url = f"{base}?{urlencode(params)}"
        self.auth_state.auth_url = auth_url

        # 启动本地回调监听（收到的码存到 _auth_code_result）
        self._start_callback_server_once()
        webbrowser.open(auth_url)
        return auth_url

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def list_tools(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise MCPError("未连接 MCP（先在网页点击「连接」）")
        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "schema": (t.inputSchema or {})}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        if not self._session:
            raise MCPError("未连接 MCP（先在网页点击「连接」）")
        result = await self._session.call_tool(name, arguments or {})
        texts = [c.text for c in (result.content or []) if getattr(c, "type", "") == "text"]
        raw = "\n".join(texts)
        try:
            return json.loads(raw) if raw else {"isError": result.isError}
        except json.JSONDecodeError:
            return raw or {"isError": result.isError}

    async def close(self) -> None:
        self._stop_callback_server()
        for cm in (self._session, self._streams_cm):
            if cm:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        self._session = None
        self._streams_cm = None
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
        self._http_client = None

    @property
    def connected(self) -> bool:
        return self._session is not None
