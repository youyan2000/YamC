#!/usr/bin/env python3
"""兼容 shim: 旧入口 `python yamc_cfg.py ...` → 新 CLI `yamc cfg_run`（转调同一 handler）。

等价命令:
  yamc cfg_run -d <工程根> --topology buck [--git-source <url>] [--hardc-path <路径>]
             [--no-submodule] [--no-build] [--params <yaml>] [--sdk-dir <dir>]
移除本文件即可（pyproject 已注册 yamc_cfg_run 入口）。
"""

import pathlib
import sys

# 包在 src/yamc/: 把 ../src 加进 sys.path（与经典 src-layout 官方 sklearn/libxr 一致）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from yamc.cli import cmd_cfg_run  # noqa: E402

if __name__ == "__main__":
    sys.exit(cmd_cfg_run())