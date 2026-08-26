"""cmake_integrate 幂等性: 两次注入只产生一个 BEGIN/END 块，旧版集成可检测。"""

from __future__ import annotations

from pathlib import Path

from yamc import cmake_integrate as CI


def _inject(cm: Path) -> dict:
    return CI.inject_cmake_integration(
        cm, "Middlewares/Third_Party/HardC", "st",
        ["Devices/pwm/pwm_buckboost.c", "Devices/adc/adc_dc_sampler.c"],
        "User/Application/app_main.c", "F3",
    )


def test_inject_idempotent(tmp_path: Path) -> None:
    cm = tmp_path / "CMakeLists.txt"
    cm.write_text("add_subdirectory(Core)\nadd_executable(fw main.c)\n", encoding="utf-8")

    r1 = _inject(cm)
    assert r1["inserted"] is True
    text1 = cm.read_text(encoding="utf-8")
    assert text1.count(CI.BEGIN_MARKER) == 1

    r2 = _inject(cm)
    assert r2["inserted"] is False
    text2 = cm.read_text(encoding="utf-8")
    assert text2.count(CI.BEGIN_MARKER) == 1
    assert "add_executable(fw main.c)" in text2


def test_old_integration_detected(tmp_path: Path) -> None:
    cm = tmp_path / "CMakeLists.txt"
    cm.write_text("target_sources(fw PRIVATE User/Components/pid.c)\n", encoding="utf-8")
    r = _inject(cm)
    assert r["old_integration"] is True