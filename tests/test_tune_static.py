"""yamc_tune_static 闭环: dry-run 预览 == render_config_block; apply 后 detect 读回目标 id。"""

from __future__ import annotations

from pathlib import Path

from yamc import params as P
from yamc.cli import cmd_tune_static

from test_params_io import make_workspace as _mkw


def test_tune_static(tmp_path: Path, capsys) -> None:
    root = _mkw(tmp_path)
    rc = cmd_tune_static(["-d", str(root), "--variant", "default", "--apply"])
    assert rc == 0
    target = P.find_target_file(root)
    assert P.detect_current_config(target) == "default"

    rc2 = cmd_tune_static(["-d", str(root), "--variant", "default",
                           "--set", "vref=24.0", "--dry-run"])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert ".vref = 24.0000000000f" in out

    rc3 = cmd_tune_static(["-d", str(root), "--variant", "default",
                           "--set", "pid_v.kp=3.5", "--save"])
    assert rc3 == 0
    ws = P.discover_params(root)
    cfg = next(c for c in ws["configs"] if c["config_id"] == "default")
    assert cfg["config"]["pid_v"]["kp"] == 3.5


def test_tune_static_missing_variant(tmp_path: Path, capsys) -> None:
    root = _mkw(tmp_path)
    rc = cmd_tune_static(["-d", str(root), "--variant", "nope", "--dry-run"])
    assert rc == 1
    assert "变体不存在" in capsys.readouterr().err