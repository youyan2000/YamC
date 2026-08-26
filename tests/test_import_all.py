"""导入冒烟: 全部 yamc 模块可导入（拦截包化 import 回归 + 语法错误）。"""

from __future__ import annotations

import importlib
import pytest

MODULES = [
    "engine", "gen_app", "ioc_parse", "c2000_syscfg", "cmake_integrate",
    "project_probe", "params", "build", "serial_tune", "topo", "version",
    "cli", "cubemx_generate", "ccs_generate", "scaffold", "flash_map_gen",
    "merge_firmware", "yamc_switch",
]


@pytest.mark.parametrize("mod", MODULES)
def test_import(mod: str) -> None:
    importlib.import_module(f"yamc.{mod}")


def test_import_shims() -> None:
    # 兼容 shim 必须可转调（不真正执行 CLI）
    import yamc.cli  # noqa: F401
    assert hasattr(__import__("yamc.cli", fromlist=["cmd_cfg_run"]), "cmd_cfg_run")
    assert hasattr(__import__("yamc.cli", fromlist=["cmd_ioc_parse"]), "cmd_ioc_parse")