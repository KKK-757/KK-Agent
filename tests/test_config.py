"""配置热更新与运行时覆盖测试（临时目录用工作区显式路径，避开沙盒 Temp 限制）。"""
import io
import shutil
from pathlib import Path

from agent.config import RUNTIME_KEYS, apply_runtime_overrides, load_settings


def test_runtime_keys_whitelist():
    assert "BIN_AGENT_BINANCE_API_KEY" in RUNTIME_KEYS
    assert "BIN_AGENT_BINANCE_API_SECRET" in RUNTIME_KEYS
    assert "SOME_RANDOM_KEY" not in RUNTIME_KEYS


def _fake_root() -> Path:
    d = Path(__file__).resolve().parent.parent / ".tmp" / "cfg_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_apply_overrides_updates_memory():
    import agent.config as cfg

    root = _fake_root()
    (root / ".env").write_text(
        "BIN_AGENT_TRADING_ENABLED=false\nBIN_AGENT_MAX_ORDER_NOTIONAL=100\n", encoding="utf-8"
    )
    old_root, cfg.ROOT = cfg.ROOT, root
    try:
        applied = apply_runtime_overrides(
            {"BIN_AGENT_TRADING_ENABLED": "true", "BIN_AGENT_MAX_ORDER_NOTIONAL": "55"}
        )
        assert applied["BIN_AGENT_TRADING_ENABLED"] == "true"
        # 就地更新：load_settings 缓存的同一实例立即生效
        cur = load_settings()
        assert cur.risk.trading_enabled is True
        assert cur.risk.max_order_notional == 55.0
        # 落盘验证
        text = (root / ".env").read_text(encoding="utf-8")
        assert "BIN_AGENT_TRADING_ENABLED=true" in text
        assert "BIN_AGENT_MAX_ORDER_NOTIONAL=55" in text
    finally:
        cfg.ROOT = old_root
        apply_runtime_overrides({"BIN_AGENT_TRADING_ENABLED": "false", "BIN_AGENT_MAX_ORDER_NOTIONAL": "100"})
        shutil.rmtree(root, ignore_errors=True)


def test_apply_overrides_rejects_unknown_keys():
    import agent.config as cfg

    applied = apply_runtime_overrides({"EVIL_KEY": "x", "BIN_AGENT_MAX_ORDER_NOTIONAL": "77"})
    assert "EVIL_KEY" not in applied
    assert "BIN_AGENT_MAX_ORDER_NOTIONAL" in applied
    apply_runtime_overrides({"BIN_AGENT_MAX_ORDER_NOTIONAL": "100"})


def test_apply_overrides_preserves_other_lines():
    import agent.config as cfg

    root = _fake_root()
    p = root / ".env"
    p.write_text("BIN_AGENT_TRADING_ENABLED=false\n# comment\nMY_CUSTOM=keepme\n", encoding="utf-8")
    old_root, cfg.ROOT = cfg.ROOT, root
    try:
        apply_runtime_overrides({"BIN_AGENT_TRADING_ENABLED": "true"})
        text = p.read_text(encoding="utf-8")
        assert "MY_CUSTOM=keepme" in text
        assert "# comment" in text
        assert "BIN_AGENT_TRADING_ENABLED=true" in text
    finally:
        cfg.ROOT = old_root
        shutil.rmtree(root, ignore_errors=True)
