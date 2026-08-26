#!/usr/bin/env python3
"""ymac_switch — 对标 xr_stm32_toolchain_switch 的 STM32 工具链切换命令.

在 STM32CubeMX CMake 工程根执行, 切换 CMakePresets.json default preset 的
toolchainFile (starm-clang.cmake <-> gcc-arm-none-eabi.cmake); clang 时再
patch cmake/starm-clang.cmake 的 STARM_TOOLCHAIN_CONFIG (标准库选择).

用法:
  ymac_switch -d <工程根> gcc
  ymac_switch -d <工程根> clang -g           # hybrid (GNU libc)
  ymac_switch -d <工程根> clang --newlib
  ymac_switch -d <工程根> clang --picolibc

对标 xr_stm32_toolchain_switch: patch CubeMX 自己的 CMakePresets.json,
不造平行构建. 缺文件/非法参数 → [FAIL] + exit 1 (与 ymac_cfg 退出码契约一致).
幂等: 已是最新则无改动且 exit 0. 重启 VSCode (CMake Presets 缓存) 后生效.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# STARM_TOOLCHAIN_CONFIG 与命令行选项的映射 (参考 STM32ToolchainSwitch.py)
_STD_MAP = {
    "g": "STARM_HYBRID",
    "gnu": "STARM_HYBRID",
    "hybrid": "STARM_HYBRID",
    "n": "STARM_NEWLIB",
    "newlib": "STARM_NEWLIB",
    "p": "STARM_PICOLIBC",
    "picolibc": "STARM_PICOLIBC",
}
_CLANG_TOOLCHAIN = "cmake/starm-clang.cmake"
_GCC_TOOLCHAIN = "cmake/gcc-arm-none-eabi.cmake"


class _SwitchError(Exception):
  """切换失败 (缺文件/缺 default preset/无 STARM 配置), main 转 [FAIL] + exit 1."""


def _info(msg: str) -> None:
  print(f"[INFO] {msg}")


def _pass(msg: str) -> None:
  print(f"[Pass] {msg}")


def _fail(msg: str) -> None:
  print(f"[FAIL] {msg}")


def _patch_cmakepresets(project_dir: Path, compiler: str) -> bool:
  """Patch default preset toolchainFile. 失败抛 _SwitchError; 返回是否有改动."""
  presets_path = project_dir / "CMakePresets.json"
  if not presets_path.exists():
    raise _SwitchError(f"{presets_path} 不存在 — 需 CubeMX 导出的 CMake 工程 (含 CMakePresets.json)")
  try:
    presets = json.loads(presets_path.read_text(encoding="utf-8-sig"))
  except json.JSONDecodeError as exc:
    raise _SwitchError(f"{presets_path} 不是合法 JSON: {exc}")
  default_preset = next((p for p in presets.get("configurePresets", []) if p.get("name") == "default"), None)
  if default_preset is None:
    raise _SwitchError("CMakePresets.json 无 'default' preset (需 Debug/Release 继承它)")
  new_toolchain = _CLANG_TOOLCHAIN if compiler == "clang" else _GCC_TOOLCHAIN
  expected = "${sourceDir}/" + new_toolchain
  if default_preset.get("toolchainFile") == expected:
    _info(f"default preset 已是 {new_toolchain}, 无需改动")
    return False
  default_preset["toolchainFile"] = expected
  presets_path.write_text(json.dumps(presets, indent=4) + "\n", encoding="utf-8")
  _info(f"default preset toolchainFile → {new_toolchain}")
  return True


def _patch_clang_stdlib(project_dir: Path, starm_config: str) -> bool:
  """Patch starm-clang.cmake 的 STARM_TOOLCHAIN_CONFIG. 失败抛 _SwitchError."""
  cmake_file = project_dir / _CLANG_TOOLCHAIN
  if not cmake_file.exists():
    raise _SwitchError(f"{cmake_file} 不存在 — clang 切换需要 starm-clang.cmake")
  pat = re.compile(r'set\s*\(\s*STARM_TOOLCHAIN_CONFIG\s+"(.*?)"\s*\)')
  lines = cmake_file.read_text(encoding="utf-8").splitlines(keepends=True)
  for i, line in enumerate(lines):
    m = pat.search(line)
    if m:
      if m.group(1) == starm_config:
        _info(f"STARM_TOOLCHAIN_CONFIG 已是 {starm_config}")
        return False
      lines[i] = f'set(STARM_TOOLCHAIN_CONFIG "{starm_config}")\n'
      cmake_file.write_text("".join(lines), encoding="utf-8")
      _info(f"STARM_TOOLCHAIN_CONFIG → {starm_config}")
      return True
  raise _SwitchError(f"{cmake_file} 中找不到 set(STARM_TOOLCHAIN_CONFIG ...)")


def main(argv: Optional[list[str]] = None) -> int:
  ap = argparse.ArgumentParser(
      description="STM32 工具链切换 (对标 xr_stm32_toolchain_switch): "
                  "patch CMakePresets.json 的 default preset toolchainFile.",
      formatter_class=argparse.RawTextHelpFormatter,
  )
  ap.add_argument("-d", "--dir", default=".", help="STM32CubeMX CMake 工程根 (含 CMakePresets.json)")
  ap.add_argument("compiler", choices=["gcc", "clang"], help="工具链: gcc 或 clang")
  group = ap.add_mutually_exclusive_group()
  group.add_argument("-g", "--gnu", "--hybrid", dest="std", action="store_const", const="hybrid",
                     help="clang: hybrid (GNU libc)")
  group.add_argument("-n", "--newlib", dest="std", action="store_const", const="newlib",
                     help="clang: newlib")
  group.add_argument("-p", "--picolibc", dest="std", action="store_const", const="picolibc",
                     help="clang: picolibc")
  args = ap.parse_args(argv)

  project_dir = Path(args.dir)
  if not project_dir.is_dir():
    _fail(f"{project_dir} 不存在")
    return 1

  # 用法错误 (参数组合非法) — 直接 [FAIL] + usage, 不触碰任何文件
  if args.compiler == "gcc" and args.std:
    _fail("-g/-n/-p 是 clang 选项, gcc 用默认标准库")
    ap.print_usage()
    return 1
  if args.compiler == "clang" and not args.std:
    _fail("clang 必须指定标准库: -g/--hybrid, -n/--newlib, -p/--picolibc")
    ap.print_usage()
    return 1

  try:
    if args.compiler == "gcc":
      if not (project_dir / _GCC_TOOLCHAIN).exists():
        raise _SwitchError(f"{project_dir / _GCC_TOOLCHAIN} 不存在 — gcc 切换需要该工具链文件")
      _patch_cmakepresets(project_dir, "gcc")
      _pass("工具链 → GCC (arm-none-eabi), 重启 VSCode 生效")
      return 0

    # clang: 必须显式选标准库 (STARM_TOOLCHAIN_CONFIG 决定 link 的 libc).
    # 先校验工具链文件存在再动 preset (避免半途状态), 两条 patch 都要跑 —
    # toolchainFile 已对不意味 STARM_TOOLCHAIN_CONFIG 已对.
    if not (project_dir / _CLANG_TOOLCHAIN).exists():
      raise _SwitchError(f"{project_dir / _CLANG_TOOLCHAIN} 不存在 — clang 切换需要该工具链文件")
    _patch_cmakepresets(project_dir, "clang")
    _patch_clang_stdlib(project_dir, _STD_MAP[args.std])
    _pass(f"工具链 → Clang ({_STD_MAP[args.std]}), 重启 VSCode 生效")
    return 0
  except _SwitchError as exc:
    _fail(str(exc))
    return 1


if __name__ == "__main__":
  sys.exit(main())
