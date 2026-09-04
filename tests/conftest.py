"""pytest 全局配置：临时目录重定向到工作区（沙盒限制系统 Temp 的写入模式）。"""
import tempfile
from pathlib import Path

_tmp = Path(__file__).resolve().parent.parent / ".tmp" / "pytest"
_tmp.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_tmp)
