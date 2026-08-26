"""engine/CLI 端到端: 假 CubeMX 工程 + 假 hardc 库根 → cfg_run / gen_code / cmake_inject 全链路。

golden 对照: cfg_run 与 各 stop_after 阶段的产物关系 (CMake 块只在 step≥6 出现)。
"""

from __future__ import annotations

from pathlib import Path

from yamc import cmake_integrate as CI
from yamc.cli import cmd_cfg_run, cmd_cmake_inject, cmd_gen_code

TOPOLOGY = """\
name: buck
status: ready
description: e2e fake topo
control_module: Module/unknown_ctl
pwm:
  device: pwm_buckboost
  freq_hz: 100000
adc:
  device: adc_dc_sampler
modules:
  - components/pid
"""

APP_C_TMPL = """\
#include "app_main.h"
/* YMAC BSP BEGIN */
/* YMAC BSP END */
/* CONFIG BEGIN */
/* CONFIG END */
void app_main(void) {}
"""

APP_H_TMPL = "#pragma once\n"

IOC = """\
Mcu.Family=STM32F3
Mcu.Name=STM32F334R8Tx
ProjectManager.ProjectName=demo
"""


def make_fake_hardc(tmp: Path) -> Path:
    hardc = tmp / "hardc"
    (hardc / "cmake").mkdir(parents=True)
    (hardc / "cmake" / "HardC.CMake").write_text("# fake\n", encoding="utf-8")
    (hardc / "Config" / "topologies").mkdir(parents=True)
    (hardc / "Config" / "topologies" / "buck.yaml").write_text(TOPOLOGY, encoding="utf-8")
    (hardc / "App").mkdir(parents=True)
    (hardc / "App" / "app_main.c.tmpl").write_text(APP_C_TMPL, encoding="utf-8")
    (hardc / "App" / "app_main.h.tmpl").write_text(APP_H_TMPL, encoding="utf-8")
    return hardc


def make_fake_project(tmp: Path) -> Path:
    proj = tmp / "proj"
    (proj / "Core" / "Inc").mkdir(parents=True)
    (proj / "demo.ioc").write_text(IOC, encoding="utf-8")
    (proj / "CMakeLists.txt").write_text("add_subdirectory(Core)\n", encoding="utf-8")
    return proj


def _opts(proj: Path, hardc: Path) -> list[str]:
    return ["-d", str(proj), "-t", "buck",
            "--no-submodule", "--hardc-path", str(hardc), "--no-build"]


def test_cfg_run_e2e(tmp_path: Path) -> None:
    hardc = make_fake_hardc(tmp_path)
    proj = make_fake_project(tmp_path)
    rc = cmd_cfg_run(_opts(proj, hardc))
    assert rc == 0
    app_c = proj / "User" / "Application" / "app_main.c"
    assert app_c.is_file()
    cm = proj / "CMakeLists.txt"
    cm_text = cm.read_text(encoding="utf-8")
    assert cm_text.count(CI.BEGIN_MARKER) == 1
    # periph 缓存落盘
    assert list((proj / ".hardc").glob("*.periph.yaml"))


def test_gen_code_stops_before_cmake(tmp_path: Path) -> None:
    hardc = make_fake_hardc(tmp_path)
    proj = make_fake_project(tmp_path)
    rc = cmd_gen_code(_opts(proj, hardc))
    assert rc == 0
    assert (proj / "User" / "Application" / "app_main.c").is_file()
    # stop_after=4: CMake 块不应出现
    assert CI.BEGIN_MARKER not in proj.joinpath("CMakeLists.txt").read_text(encoding="utf-8")


def test_cmake_inject_step(tmp_path: Path) -> None:
    hardc = make_fake_hardc(tmp_path)
    proj = make_fake_project(tmp_path)
    rc = cmd_cmake_inject(_opts(proj, hardc))
    assert rc == 0
    text = proj.joinpath("CMakeLists.txt").read_text(encoding="utf-8")
    assert text.count(CI.BEGIN_MARKER) == 1

def test_default_hardc_git_source_contract() -> None:
    """陌生工程默认自动拉取官方 hardc（问题3核心契约）。"""
    from yamc import engine as E
    assert E.DEFAULT_HARDC_GIT_SOURCE == "https://github.com/youyan2000/HardC.git"

    # run_pipeline 缺省 opts(无 git_source) → 用默认 URL; no_submodule=True → 不自动拉取
    # 直接验证逻辑分支: 默认(非 no_submodule)不置 git_source=None
    import inspect
    src = inspect.getsource(E.run_pipeline)
    assert "DEFAULT_HARDC_GIT_SOURCE" in src, "run_pipeline 应引用默认 URL"


def test_probe_all_reports_auto_import(tmp_path: Path) -> None:
    """无本地 hardc 的目录 → hardc_auto_import=True（不再死报未找到）。"""
    import os
    from yamc import project_probe as PP
    # 隔离 env, 指向一个不含 hardc 的临时目录
    old = os.environ.get("HARDC_LIB_DIR")
    os.environ.pop("HARDC_LIB_DIR", None)
    try:
        info = PP.probe_all(tmp_path)
    finally:
        if old:
            os.environ["HARDC_LIB_DIR"] = old
        else:
            os.environ.pop("HARDC_LIB_DIR", None)
    assert info.get("hardc") is None
    assert info.get("hardc_auto_import") is True, "应标记可自动导入"
    # 无工程根时 reason 是"未找到工程根"，hardc_auto_import 才是自动导入的真相源