"""cubemx_generate: 脚本构造 / 命令构造 / 缺 .ioc 错误路径（对标 cubemx_runner_smoke 思路）。

FAKE CubeMX 用临时 python 可执行文件模拟（Linux shebang；Windows 上跳过进程执行部分）。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from yamc import cubemx_generate as CG

FAKE_CUBEMX = r"""
import sys, pathlib, os
if "--q" not in sys.argv and "-q" not in sys.argv:
    print("missing -q", file=sys.stderr); sys.exit(2)
script = None
for a in sys.argv:
    if a.startswith(("-q", "--q")):
        continue
    if a == "-s":
        continue
    if a.endswith(".iocscript") or "script" in a:
        script = a
if not script:
    sys.exit(3)
os.makedirs("Core/Inc", exist_ok=True)
os.makedirs("Drivers", exist_ok=True)
print("fake CubeMX OK")
sys.exit(0)
"""


def _write_fake(tmp: Path) -> Path:
    fake = tmp / "fake_cubemx"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_CUBEMX, encoding="utf-8")
    if os.name != "nt":
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def test_build_script() -> None:
    ioc = Path("/tmp/demo.ioc")
    script = CG.build_cubemx_script(ioc)
    assert f"load project {ioc.resolve().as_posix()}" in script
    assert "project generate" in script


def test_build_command_jar_mode(tmp_path: Path) -> None:
    jar = tmp_path / "stm32cubemx.jar"
    jar.write_bytes(b"PK")  # 假 jar（函数只校验存在性）
    script = tmp_path / "s.iocscript"
    script.write_text("", encoding="utf-8")
    cmd = CG.build_cubemx_command(script, cubemx_cmd=str(jar),
                                  launch_mode="java", java_cmd="java")
    assert cmd[0] == "java"
    assert "-jar" in cmd
    assert cmd[-2:] == ["-s", str(script)]


def test_missing_ioc_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CG.generate_cubemx_project(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="fake runner 用 shebang 脚本, 仅 POSIX 环境执行")
def test_generate_with_fake_cubemx(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "demo.ioc").write_text("ProjectManager.ProjectName=demo\n", encoding="utf-8")
    fake = _write_fake(tmp_path)
    r = CG.generate_cubemx_project(project, cubemx_cmd=str(fake), timeout=60)
    assert r["ok"] is True
    assert (project / "Core" / "Inc").is_dir()
    assert (project / "Drivers").is_dir()