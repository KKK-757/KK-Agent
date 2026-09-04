#!/usr/bin/env python
"""pre-commit 钩子：阻止 .env / 密钥文件被提交到 git。

安装：python install_hook.py   （把本脚本配置为 .git/hooks/pre-commit）
"""
import re
import sys
from pathlib import Path

SECRET_PATTERNS = [
    (r"\.env$", "环境变量文件（含 API Key）"),
    (r"\.pem$", "私钥文件"),
    (r"\.key$", "密钥文件"),
    (r"watchlist\.json$", "本地自选列表（非敏感但属个人数据）"),
]

# 内容级检测：疑似硬编码密钥（长度>=20 的 base64 样式串出现在要提交的文件里）
CONTENT_PATTERNS = [
    (r"(?i)(binance)?_?(api)?_?secret\s*=\s*['\"]?[A-Za-z0-9]{20,}", "疑似 API Secret 硬编码"),
    (r"(?i)binance_api_key\s*=\s*['\"]?[A-Za-z0-9]{30,}", "疑似 API Key 硬编码"),
]


def main() -> int:
    staged = Path(".git/hooks/../..")
    # 取暂存文件列表
    import subprocess

    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return 0  # 非 git 环境，放行
    files = [f for f in out.splitlines() if f.strip()]
    blocked = []

    for f in files:
        for pat, why in SECRET_PATTERNS:
            if re.search(pat, f):
                blocked.append(f"{f}  <- {why}")
        # 内容级检查（仅文本文件）
        p = Path(f)
        if p.exists() and p.stat().st_size < 1_000_000:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
                for pat, why in CONTENT_PATTERNS:
                    if re.search(pat, text):
                        blocked.append(f"{f}  <- {why}")
            except Exception:
                pass

    if blocked:
        print("✋ pre-commit: 检测到疑似密钥文件/内容，已阻止提交：\n  " + "\n  ".join(blocked))
        print("\n如确认为误报，可用 git commit --no-verify 跳过（请慎重）。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
