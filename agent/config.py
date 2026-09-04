"""Configuration loading.

Non-sensitive behavior configuration comes from config.yaml;
sensitive information (Keys, switches) comes only from environment variables / .env, never written into code or logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

OFFICIAL_MCP_ENDPOINT = "https://agent.binance.com/mcp/agentic"


@dataclass
class LLMConfig:
    provider: str = "mock"          # deepseek/openai/anthropic/glm/moonshot/qwen/compatible/mock
    api_key: str = ""               # [SLOT] User fills this in later
    base_url: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048

    @property
    def ready(self) -> bool:
        """mock is always ready; other providers require a Key."""
        return self.provider == "mock" or bool(self.api_key)


@dataclass
class BinanceConfig:
    api_base: str = "https://api.binance.com"
    trade_base: str = "https://testnet.binance.vision"
    recv_window: int = 5000
    api_key: str = ""               # [SLOT] 签名 API Key（测试网或正式盘）
    api_secret: str = ""            # [SLOT] 签名 Secret

    @property
    def keys_present(self) -> bool:
        """是否有 Key（测试网和正式盘都需要）。"""
        return bool(self.api_key and self.api_secret)

    @property
    def is_testnet(self) -> bool:
        """当前交易环境是否为模拟盘（测试网）。下单去向由此决定。"""
        return "testnet" in self.trade_base.lower()


@dataclass
class RiskConfig:
    trading_enabled: bool = False          # 下单总开关（测试网/正式盘共用）
    max_order_notional: float = 100.0      # 单笔限额（USDT）
    symbol_blacklist: tuple = ()           # 禁止币种
    confirm_trades: bool = True            # 正式盘下单前要求 CLI 手动确认
    forbidden_actions: tuple = ("withdraw", "transfer")   # 永不执行的操作


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    allow_real_trades: bool = False        # Hard-coded: Web always disallows real trading


@dataclass
class AgentSettings:
    llm: LLMConfig = field(default_factory=LLMConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    web: WebConfig = field(default_factory=WebConfig)
    mcp_endpoint: str = OFFICIAL_MCP_ENDPOINT
    log_level: str = "INFO"
    persona: str = ""
    agent_name: str = "KK-Agent"
    market: Dict[str, Any] = field(default_factory=dict)
    signals: Dict[str, Any] = field(default_factory=dict)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _ensure_env_file() -> None:
    """首次运行自动初始化：clone 后没有 .env 时，从 .env.example 复制一份。"""
    env_file = ROOT / ".env"
    if env_file.exists():
        return
    example = ROOT / ".env.example"
    if example.exists():
        env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


# 运行时可热更新的敏感配置（网页端设置面板写入 .env 并同步到内存）
RUNTIME_KEYS = (
    "BIN_AGENT_LLM_PROVIDER", "BIN_AGENT_LLM_API_KEY", "BIN_AGENT_LLM_BASE_URL",
    "BIN_AGENT_LLM_MODEL", "BIN_AGENT_BINANCE_API_KEY", "BIN_AGENT_BINANCE_API_SECRET",
    "BIN_AGENT_BINANCE_TRADE_BASE", "BIN_AGENT_TRADING_ENABLED",
    "BIN_AGENT_MAX_ORDER_NOTIONAL",
)


def apply_runtime_overrides(overrides: Dict[str, str]) -> Dict[str, str]:
    """把网页端提交的 Key/开关写入 .env（不覆盖文件中的其他行）并同步到内存配置。

    返回实际应用的键值（供回显确认）。只接受 RUNTIME_KEYS 白名单内的键。
    """
    applied: Dict[str, str] = {}
    env_path = ROOT / ".env"

    # 1) 读现有 .env 行
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        example = ROOT / ".env.example"
        if example.exists():
            lines = example.read_text(encoding="utf-8").splitlines()

    clean = {k: str(v).strip() for k, v in overrides.items() if k in RUNTIME_KEYS}

    # 2) 逐行替换或标记缺失
    seen: set = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in clean:
            lines[i] = f"{key}={clean[key]}"
            seen.add(key)
    for key, val in clean.items():
        if key not in seen:
            lines.append(f"{key}={val}")

    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # 3) 同步到内存（当前进程立即生效）
    for key, val in clean.items():
        os.environ[key] = val

    # 4) 就地修改被全局引用的 settings 单例（agent/risk/trader/llm 都持有它，立即全生效）
    s = load_settings()
    if "BIN_AGENT_BINANCE_API_KEY" in clean:
        s.binance.api_key = clean["BIN_AGENT_BINANCE_API_KEY"]
    if "BIN_AGENT_BINANCE_API_SECRET" in clean:
        s.binance.api_secret = clean["BIN_AGENT_BINANCE_API_SECRET"]
    if "BIN_AGENT_BINANCE_TRADE_BASE" in clean:
        s.binance.trade_base = clean["BIN_AGENT_BINANCE_TRADE_BASE"]
    if "BIN_AGENT_TRADING_ENABLED" in clean:
        s.risk.trading_enabled = clean["BIN_AGENT_TRADING_ENABLED"].lower() == "true"
    if "BIN_AGENT_MAX_ORDER_NOTIONAL" in clean:
        try:
            s.risk.max_order_notional = float(clean["BIN_AGENT_MAX_ORDER_NOTIONAL"])
        except ValueError:
            pass
    if "BIN_AGENT_LLM_API_KEY" in clean:
        s.llm.api_key = clean["BIN_AGENT_LLM_API_KEY"]
    if "BIN_AGENT_LLM_PROVIDER" in clean:
        s.llm.provider = clean["BIN_AGENT_LLM_PROVIDER"].lower()
    if "BIN_AGENT_LLM_BASE_URL" in clean:
        s.llm.base_url = clean["BIN_AGENT_LLM_BASE_URL"]
    if "BIN_AGENT_LLM_MODEL" in clean:
        s.llm.model = clean["BIN_AGENT_LLM_MODEL"]

    # webapp 的 trader.base 缓存了旧地址，由调用方 rebind（见 webapp.save_keys）
    return clean


@lru_cache(maxsize=1)
def load_settings() -> AgentSettings:
    """Read .env + config.yaml, env takes precedence. Called on every process start."""
    _ensure_env_file()   # 首次运行自动从 .env.example 创建 .env
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / ".env.example", override=False)

    cfg_path = ROOT / "config.yaml"
    yml: Dict[str, Any] = {}
    if cfg_path.exists():
        yml = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    s = AgentSettings()

    s.llm = LLMConfig(
        provider=_env("BIN_AGENT_LLM_PROVIDER", "mock").lower(),
        api_key=_env("BIN_AGENT_LLM_API_KEY"),
        base_url=_env("BIN_AGENT_LLM_BASE_URL"),
        model=_env("BIN_AGENT_LLM_MODEL"),
    )

    s.binance = BinanceConfig(
        api_base=_env("BIN_AGENT_BINANCE_API_BASE", "https://api.binance.com"),
        trade_base=_env("BIN_AGENT_BINANCE_TRADE_BASE", "https://api.binance.com"),
    )
    s.binance.api_key = _env("BIN_AGENT_BINANCE_API_KEY")
    s.binance.api_secret = _env("BIN_AGENT_BINANCE_API_SECRET")

    s.risk = RiskConfig(
        trading_enabled=_env("BIN_AGENT_TRADING_ENABLED", "false").lower() == "true",
        max_order_notional=float(_env("BIN_AGENT_MAX_ORDER_NOTIONAL", "100") or 100),
        symbol_blacklist=tuple(
            x.strip().upper() for x in _env("BIN_AGENT_SYMBOL_BLACKLIST").split(",") if x.strip()
        ),
        confirm_trades=_env("BIN_AGENT_CONFIRM_TRADES", "true").lower() == "true",
    )
    forbidden = yml.get("risk", {}).get("forbidden_actions") or []
    if forbidden:
        s.risk.forbidden_actions = tuple(str(x).lower() for x in forbidden)

    s.web = WebConfig(
        host=_env("BIN_AGENT_WEB_HOST", "127.0.0.1"),
        port=int(_env("BIN_AGENT_WEB_PORT", "8787") or 8787),
        allow_real_trades=False,  # Hard-coded: no real trading on Web
    )

    s.mcp_endpoint = _env("BIN_AGENT_MCP_ENDPOINT", OFFICIAL_MCP_ENDPOINT)
    if s.mcp_endpoint != OFFICIAL_MCP_ENDPOINT:
        # Anti-phishing: do not silently accept a replaced endpoint; only warn at load time (raise at execution time)
        s.mcp_endpoint = OFFICIAL_MCP_ENDPOINT

    s.log_level = _env("BIN_AGENT_LOG_LEVEL", "INFO").upper()

    agent_yml = yml.get("agent", {}) or {}
    s.agent_name = agent_yml.get("name", s.agent_name)
    s.persona = (agent_yml.get("persona") or "").strip()
    s.market = yml.get("market", {}) or {}
    s.signals = yml.get("signals", {}) or {}
    return s
