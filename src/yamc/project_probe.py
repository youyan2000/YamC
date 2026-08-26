"""yamc_cfg — 平台探测: 定位工程根 + 检测平台 (stm32 / c2000).

对标 xr_cubemx_cfg: 在外部工程根目录执行一条命令, 自动完成 HardC 接入.
本模块只做探测 (纯函数), CLI/GUI 共享.

平台判定规则:
  - *.ioc 存在            → stm32  (STM32CubeMX)
  - main.syscfg 存在      → c2000  (TI SysConfig, CCS)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

# 工程特征文件, 按优先级判定. 只支持 stm32 + c2000 (平台由用户指定, 不引入其他平台).
# stm32: 任意 *.ioc (CubeMX 工程名随意, 如 software.ioc).
# c2000: main.syscfg (CCS/SysConfig) 或 makefile.defs.
_PLATFORM_MARKERS = (
    ("stm32", ("*.ioc",)),
    ("c2000", ("main.syscfg", "makefile.defs")),
)


def _glob_hit(root: Path, pattern: str) -> bool:
    try:
        return any(root.glob(pattern))
    except OSError:
        return False


def _has_marker(root: Path) -> bool:
    if not root.is_dir():
        return False
    for _platform, markers in _PLATFORM_MARKERS:
        for m in markers:
            if _glob_hit(root, m):
                return True
    return False


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """从 start (默认 cwd) 向上找工程根: 首个含平台特征文件的目录."""
    cur = Path(start or Path.cwd()).resolve()
    for _ in range(12):
        if _has_marker(cur):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def detect_platform(root: Path) -> Optional[str]:
    """返回平台名 ('stm32'/'c2000'), 检测不到返回 None."""
    if not root.is_dir():
        return None
    for platform, markers in _PLATFORM_MARKERS:
        if all(_glob_hit(root, m) for m in markers):
            return platform
        # c2000 宽松判定: main.syscfg 或 RELEASE/ 产物目录
        if platform == "c2000":
            if (root / "main.syscfg").is_file():
                return "c2000"
    return None


def find_ioc(root: Path) -> Optional[Path]:
    """返回工程根下第一个 .ioc 文件 (STM32)."""
    for p in sorted(root.rglob("*.ioc")):
        if "build" not in p.parts:
            return p
    return None


def _read_ioc(path: Path) -> dict:
    """解析 .ioc (KEY=value, `\\#`/`\\:` 转义) → dict."""
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.replace("\\#", "#").replace("\\:", ":").strip()
        val = val.replace("\\:", ":").strip()
        out[key] = val
    return out


def _to_int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s = s.strip().split("-")[0]  # "720-1" → "720"
    try:
        return int(s)
    except ValueError:
        return None


DEFAULT_HARDC_REL = "Middlewares/Third_Party/HardC"


def probe_hardc_lib(start: Optional[Path] = None, hardc_path: Optional[Path] = None) -> Optional[Path]:
    """静态定位 hardc 库根（不做 git 操作）。

    优先级: 显式 --hardc-path → 环境变量 HARDC_LIB_DIR → 同级 ../hardc → 工程 submodule。
    """
    if hardc_path is not None:
        p = Path(hardc_path).resolve()
        if (p / "cmake" / "HardC.CMake").is_file():
            return p
        return None
    env_lib = os.environ.get("HARDC_LIB_DIR")
    if env_lib and (Path(env_lib) / "cmake" / "HardC.CMake").is_file():
        return Path(env_lib).resolve()
    start = Path(start or os.getcwd()).resolve()
    sibling = start / "hardc"
    if (sibling / "cmake" / "HardC.CMake").is_file():
        return sibling.resolve()
    # 从起始目录向上找同级 hardc（仓库平铺时 start 通常是 yamc/）
    for parent in [start] + list(start.parents):
        cand = parent / "hardc"
        if (cand / "cmake" / "HardC.CMake").is_file():
            return cand.resolve()
    sub = start / DEFAULT_HARDC_REL
    if (sub / "cmake" / "HardC.CMake").is_file():
        return sub.resolve()
    return None


def probe_all(start: Optional[Path] = None, hardc_path: Optional[Path] = None) -> dict:
    """一站式探测（yamc_probe / yamc_check 的数据源）:

    工程根探测与 hardc/工具链探测解耦: 没有工程根也能报出 hardc 库根与工具链。
    返回 {ok, root, platform, ioc, syscfg, hardc, cmake, git, toolchains, reason?}
    ok = 找到 hardc 库根（多数命令的依赖项）。
    """
    out: dict = {"ok": False}
    start = Path(start or os.getcwd()).resolve()
    root = find_project_root(start)
    out["root"] = str(root) if root else None
    out["platform"] = None  # 无工程根时也保有键（cmd_probe 依此降级输出）
    if root is None:
        out["reason"] = f"未找到工程根 (从 {start} 向上找 .ioc/.syscfg) — 多数命令可加 -d/--root 显式给出"
    else:
        platform = detect_platform(root)
        out["platform"] = platform
        if platform == "stm32":
            ioc = find_ioc(root)
            out["ioc"] = str(ioc.relative_to(root)) if ioc else None
            out["syscfg"] = None
        elif platform == "c2000":
            out["ioc"] = None
            syscfg = root / "main.syscfg"
            out["syscfg"] = str(syscfg.relative_to(root)) if syscfg.is_file() else None
        else:
            out["reason"] = f"{root} 未识别平台 (需 .ioc / main.syscfg)"

    hardc = probe_hardc_lib(root if root else start, hardc_path)
    out["hardc"] = str(hardc) if hardc else None
    out["cmake"] = shutil.which("cmake")
    out["git"] = shutil.which("git")

    toolchains: dict[str, Optional[str]] = {}
    pivot = hardc or start
    for name in ("starm-clang.cmake", "gcc-arm-none-eabi.cmake", "c2000-ti-cgt.cmake"):
        p = pivot / "cmake" / name
        toolchains[name] = str(p) if p.is_file() else None
    out["toolchains"] = toolchains

    if hardc is not None:
        out["ok"] = True
    elif not out.get("reason"):
        out["reason"] = "未找到 hardc 库根: 设 HARDC_LIB_DIR 或用 --hardc-path"
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(probe_all(), ensure_ascii=False, indent=2, default=str))
