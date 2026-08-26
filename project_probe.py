"""yamc_cfg — 平台探测: 定位工程根 + 检测平台 (stm32 / c2000).

对标 xr_cubemx_cfg: 在外部工程根目录执行一条命令, 自动完成 HardC 接入.
本模块只做探测 (纯函数), CLI/GUI 共享.

平台判定规则:
  - *.ioc 存在            → stm32  (STM32CubeMX)
  - main.syscfg 存在      → c2000  (TI SysConfig, CCS)
"""

from __future__ import annotations

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


if __name__ == "__main__":
    r = find_project_root()
    if not r:
        print(f"[FAIL] 未找到工程根 (从 {Path.cwd()} 向上找特征文件)", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] 工程根: {r}")
    print(f"[INFO] 平台:   {detect_platform(r)}")
