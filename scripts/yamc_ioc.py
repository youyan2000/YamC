#!/usr/bin/env python3
"""兼容 shim: 旧入口 `python yamc_ioc.py ...` → 新 CLI `yamc ioc_parse`（转调同一 handler）。

等价命令:
  yamc ioc_parse -d <工程根> [-o <out.yaml>] [--force] [--verbose]
移除本文件即可（pyproject 已注册 yamc_ioc_parse 入口）。
"""

import pathlib
import sys

# 包在 src/yamc/: 把 ../src 加进 sys.path（经典 src-layout，与 libxr 一致）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from yamc.cli import cmd_ioc_parse  # noqa: E402

if __name__ == "__main__":
    sys.exit(cmd_ioc_parse())