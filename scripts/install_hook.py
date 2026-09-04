"""一键安装 git pre-commit 钩子。"""
import shutil
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
hook_src = ROOT / "scripts" / "pre_commit_hook.py"
hook_dst = ROOT / ".git" / "hooks" / "pre-commit"

if not (ROOT / ".git").exists():
    print("不是 git 仓库，跳过（开源后用户 clone 下来再装也行）")
    raise SystemExit(0)

hook_dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copy(hook_src, hook_dst)
# 加执行位（Windows 下无实际意义，但 clone 到 Linux/Mac 时需要）
hook_dst.chmod(hook_dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
print(f"已安装 pre-commit 钩子: {hook_dst}")
print("效果：git commit 时自动拦截 .env / 密钥文件 / 疑似硬编码密钥。")
