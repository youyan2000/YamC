"""CLI 命令树: 每个 `yamc_*` / 伞命令 `yamc <tool> <action>` 的 --help 均退出 0。"""

from __future__ import annotations

import pytest

from yamc import cli
from yamc.cli import umbrella_main

TOOLS = sorted(cli._TOOL_TABLE.keys())


@pytest.mark.parametrize("tool", [t for t in TOOLS if t != "cfg"])  # cfg 是 cfg_run 别名
def test_tool_help(tool: str) -> None:
    """argparse 工具 --help → SystemExit(0)；check/gui 自行处理 → 返回 0。"""
    try:
        rc = cli._TOOL_TABLE[tool](["--help"])
        assert rc == 0
    except SystemExit as exc:
        assert exc.code == 0


def test_umbrella_help_and_version() -> None:
    assert umbrella_main(["--help"]) == 0
    assert umbrella_main(["--version"]) == 0
    assert umbrella_main([]) == 0  # 无参数 → 打印用法


def test_umbrella_two_word_alias() -> None:
    """`yamc ioc parse --help` ≡ `yamc_ioc_parse --help`。"""
    with pytest.raises(SystemExit) as exc:
        umbrella_main(["ioc", "parse", "--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc2:
        umbrella_main(["tune", "static", "--help"])
    assert exc2.value.code == 0


def test_umbrella_unknown_tool() -> None:
    assert umbrella_main(["nonexistent_tool"]) == 2