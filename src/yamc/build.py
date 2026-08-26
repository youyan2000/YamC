"""yamc/build — 构建系统检测与编译命令（无 Qt 依赖）。

从 yaml_config_builder.py 顶层原样抽出：GUI 编译按钮与 yamc_build 共用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def detect_build(project_root: Path) -> Optional[dict]:
    """自动检测构建系统。支持 CMake（build/ 子目录）。"""
    for build_dir_name in ("build", "build/Debug", "build/Release"):
        bd = project_root / build_dir_name
        if (bd / "CMakeCache.txt").is_file():
            return {
                "type": "cmake",
                "dir": str(bd),
                "label": build_dir_name,
            }
    build_root = project_root / "build"
    if build_root.is_dir():
        for child in sorted(build_root.iterdir()):
            if child.is_dir() and (child / "CMakeCache.txt").is_file():
                return {
                    "type": "cmake",
                    "dir": str(child),
                    "label": f"build/{child.name}",
                }
    return None


def find_cmake() -> Optional[str]:
    """查找 cmake 可执行文件路径。

    先尝试 PATH，再搜索常见安装位置（含 ARM 工具链）。
    """
    import shutil

    cmake = shutil.which("cmake")
    if cmake:
        return cmake

    # 同时尝试搜索所有盘符下的常见工具链目录
    candidates: list[str] = []

    if sys.platform == "win32":
        # 扫描所有盘符下的常见目录
        import glob as _glob
        drive_patterns = [
            r"{drive}:\Program Files\CMake\bin\cmake.exe",
            r"{drive}:\Program Files (x86)\CMake\bin\cmake.exe",
            r"{drive}:\GNU_C_Compiler\bin\cmake.exe",
            r"{drive}:\GNU Arm Embedded Toolchain\bin\cmake.exe",
            r"{drive}:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*\tools\bin\cmake.exe",
        ]
        # 扫描 A-Z 盘符
        import string as _string
        for letter in _string.ascii_uppercase:
            for pattern in drive_patterns:
                p = pattern.format(drive=letter)
                # 用 glob 匹配通配符路径
                if "*" in p:
                    for matched in _glob.glob(p):
                        candidates.append(matched)
                else:
                    candidates.append(p)

        # VS 2022 自带 CMake
        vs_cmake = (
            r"{drive}:\Program Files\Microsoft Visual Studio\2022"
            r"\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        )
        for letter in _string.ascii_uppercase:
            p = vs_cmake.format(drive=letter)
            candidates.append(p)

        # cmake 可能在任何 CMake* 目录下
        for letter in _string.ascii_uppercase:
            for base in [f"{letter}:\\Program Files", f"{letter}:\\Program Files (x86)"]:
                for matched in _glob.glob(f"{base}\\CMake*\\bin\\cmake.exe"):
                    candidates.append(matched)

    # Linux
    for c in ["/usr/bin/cmake", "/usr/local/bin/cmake", "/snap/bin/cmake"]:
        candidates.append(c)

    for c in candidates:
        if Path(c).is_file():
            return c

    return None


def build_command(build_info: dict) -> list[str]:
    """根据构建信息生成 cmake --build 命令。优先使用 find_cmake()。"""
    cmake_path = find_cmake() or "cmake"
    return [cmake_path, "--build", build_info["dir"]]