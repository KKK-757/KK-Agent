"""LLM abstraction layer.

- All OpenAI-compatible providers (DeepSeek / GLM / Moonshot / Qwen / OpenAI / any compatible endpoint) use one unified implementation;
- anthropic uses its native interface;
- provider=mock requires no Key at all: the Agent runs in "rule-engine mode" and can still fetch market data, compute indicators, and generate reports —
  the entire program is usable before the user fills in a Key.
"""
from __future__ import annotations

import json
import logging
from typing import List, Dict, Any, Optional

from .config import AgentSettings

log = logging.getLogger("agent.llm")

SYSTEM_FALLBACK = (
    "You are a crypto market assistant AI Agent. Answer professionally and cautiously, always remind about risks, "
    "and do not provide guarantees of returns."
)


class LLMError(RuntimeError):
    pass


class LLMClient:
    """Unified LLM entry point. When provider=mock, available=False and the caller falls back to the rule engine."""

    def __init__(self, settings: AgentSettings):
        self.s = settings.llm
        self.persona = settings.persona or SYSTEM_FALLBACK

    @property
    def available(self) -> bool:
        return self.s.ready

    @property
    def active(self) -> bool:
        """Whether the LLM can really be called (mock does not count — mock goes through the rule engine)."""
        return self.s.provider != "mock" and self.s.ready

    def describe(self) -> str:
        if self.s.provider == "mock":
            return "LLM：未配置（mock 模式）—— Agent 以规则引擎运行；在 .env 填入 BIN_AGENT_LLM_API_KEY 后自动升级为 LLM 模式"
        model = self.s.model or "(默认)"
        return f"LLM：{self.s.provider} / {model}（Key 已配置：{'是' if self.s.api_key else '否'}）"

    # ------------------------------------------------------------------
    async def chat(self, messages: List[Dict[str, str]], temperature: Optional[float] = None) -> str:
        """Multi-turn conversation; returns the assistant reply text."""
        if self.s.provider == "mock":
            raise LLMError("mock mode has no LLM; the caller should fall back to the rule engine first")
        if not self.s.api_key:
            raise LLMError("BIN_AGENT_LLM_API_KEY not filled in")

        if self.s.provider == "anthropic":
            return await self._chat_anthropic(messages)
        return await self._chat_openaiCompatible(messages, temperature)

    # ------------------------------------------------------------------
    async def _chat_openaiCompatible(self, messages: List[Dict[str, str]], temperature: Optional[float]) -> str:
        import httpx  # local import keeps mock mode dependency-free

        base = (self.s.base_url or "").rstrip("/")
        if not base:
            presets = {
                "deepseek": "https://api.deepseek.com/v1",
                "glm": "https://open.bigmodel.cn/api/paas/v4",
                "moonshot": "https://api.moonshot.cn/v1",
                "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "openai": "https://api.openai.com/v1",
            }
            base = presets.get(self.s.provider, "")
            if not base:
                raise LLMError("Please set BIN_AGENT_LLM_BASE_URL (OpenAI-compatible endpoint)")
        url = f"{base}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.s.model or self._default_model(),
            "messages": [{"role": "system", "content": self.persona}] + messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": self.s.max_tokens,
        }
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(url, json=payload, headers={"Authorization": f"Bearer {self.s.api_key}"})
            r.raise_for_status()
            data = r.json()
        return data["choices"][0]["message"]["content"]

    async def _chat_anthropic(self, messages: List[Dict[str, str]]) -> str:
        import httpx

        url = (self.s.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        headers = {"x-api-key": self.s.api_key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": self.s.model or "claude-sonnet-4-20250514",
            "max_tokens": self.s.max_tokens,
            "system": self.persona,
            "messages": messages,
        }
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []))

    def _default_model(self) -> str:
        return {
            "deepseek": "deepseek-chat",
            "openai": "gpt-4o-mini",
            "glm": "glm-4-flash",
            "moonshot": "moonshot-v1-8k",
            "qwen": "qwen-plus",
        }.get(self.s.provider, "gpt-4o-mini")


# ----------------------------------------------------------------------
# JSON intent parsing (used by the trading workflow; rule engine as fallback)
# ----------------------------------------------------------------------
async def parse_intent(llm: LLMClient, text: str) -> Optional[Dict[str, Any]]:
    """Let the LLM extract structured trade intent from natural language; returns None on failure (caller falls back to regex)."""
    if not llm.available:
        return None
    prompt = (
        "Extract the trade intent from the following sentence and output only JSON (with no other text):\n"
        '{"action":"buy|sell","symbol":"BTCUSDT","quote_qty":number}\n'
        "If there is no clear trade intent, output {\"action\":\"none\"}.\nSentence: "
        + text
    )
    try:
        raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.0)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("action"):
            return obj
    except Exception as e:  # Any failure falls back to the rule engine; intent must never be guessed wildly
        log.debug("LLM intent parsing failed, falling back to rule engine: %s", e)
    return None
