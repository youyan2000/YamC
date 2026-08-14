"""ymac_cfg — TI SysConfig main.syscfg → C2000 外设 YAML (缓存 .coop/periph.yaml).

对标 xr_cubemx_cfg 的 .ioc 解析: 提取 ePWM/ADC/CLA 实例 + SYSCLK + SysConfig 产物
目录, 供 gen_app.py 生成 C2000 版 app_main.c/h.

与 .ioc 的关键差异: C2000 的 main.syscfg 可能很薄 (powerSUITE solution 工程把外设
实例藏在 solution C 文件里), 权威实例清单要从两处合并取:
  1. SysConfig 产物 board.c   — 完整 SysConfig 工程 (含 EPWM_setTimeBasePeriod(...) 等
                                driverlib 初始化调用)
  2. solution C 文件          — powerSUITE solution 工程 (buck_hal.c 直接引用
                                EPWM<n>_BASE / ADC<x>_BASE / CLA<n>_BASE)
  clocktree.h                 — SYSCLK (bsp_init 注入 clk_hz)

输出结构 (periph):
  platform: c2000
  mcu:      {device, part, package, sysclk_hz}
  peripherals:
    epwm: [ {instance, base, timer} ]   # timer = BSP_TIMER_A..F (EPWM1..6), EPWM7/8 无
    adc:  [ {instance, base} ]          # ADCA/ADCB/ADCC
    cla:  [ {instance, base} ]          # CLA1
  syscfg:   {dir}                        # SysConfig 产物目录 (cmake_integrate 用)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import yaml

# BSP_TIMER_A..F = ePWM1..ePWM6 (bsp_pwm.h 注释). F28004x 有 ePWM7/8, 无对应 timer.
_EPWM_TO_TIMER = {
    "EPWM1": "A", "EPWM2": "B", "EPWM3": "C",
    "EPWM4": "D", "EPWM5": "E", "EPWM6": "F",
}

# 扫描 C 源时识别的外设基址宏: EPWM<n>_BASE / ADC<x>_BASE / CLA<n>_BASE
_RE_EPWM_BASE = re.compile(r"\bEPWM(\d)_BASE\b")
_RE_ADC_BASE = re.compile(r"\bADC([ABC])_BASE\b")
_RE_CLA_BASE = re.compile(r"\bCLA(\d)_BASE\b")

# main.syscfg 头注释: @v2CliArgs --device "TMS320F280049C" --package "100PZ" ...
_RE_DEVICE = re.compile(r'--device\s+"([^"]+)"')
_RE_PACKAGE = re.compile(r'--package\s+"([^"]+)"')
_RE_PART = re.compile(r'--part\s+"([^"]+)"')

# SysConfig 产物目录候选 (CCS 输出位置随构建配置漂移)
_SYSCFG_DIR_CANDIDATES = ("RELEASE/syscfg", "Debug/syscfg", "syscfg")


def parse_main_syscfg(path: Path) -> dict:
    """main.syscfg → {device, part, package} (从头注释 @cliArgs/@v2CliArgs 提取)."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    m = _RE_DEVICE.findall(text)
    if m:
        out["device"] = m[-1]  # 优先 v2CliArgs (更精确, 如 TMS320F280049C), 它在 cliArgs 之后
    m = _RE_PACKAGE.findall(text)
    if m:
        out["package"] = m[-1]  # 同上: v2CliArgs 的 --package "100PZ" 在 cliArgs 的旧 part 之后
    m = _RE_PART.search(text)
    if m:
        out["part"] = m.group(1)
    return out


def find_syscfg_dir(root: Path) -> Optional[Path]:
    """定位 SysConfig 产物目录 (RELEASE/syscfg 等); 找不到返回 None."""
    for cand in _SYSCFG_DIR_CANDIDATES:
        p = root / cand
        if (p / "board.c").is_file():
            return p
    return None


def _scan_bases(text: str) -> dict:
    """从 C 源文本提取去重后的 {epwm:[..], adc:[..], cla:[..]} (按首次出现序)."""
    seen = {"epwm": [], "adc": [], "cla": []}

    def _add(key, item):
        if item not in seen[key]:
            seen[key].append(item)

    for m in _RE_EPWM_BASE.finditer(text):
        _add("epwm", {"instance": f"EPWM{m.group(1)}", "base": f"EPWM{m.group(1)}_BASE"})
    for m in _RE_ADC_BASE.finditer(text):
        _add("adc", {"instance": f"ADC{m.group(1)}", "base": f"ADC{m.group(1)}_BASE"})
    for m in _RE_CLA_BASE.finditer(text):
        _add("cla", {"instance": f"CLA{m.group(1)}", "base": f"CLA{m.group(1)}_BASE"})
    return seen


def _scan_source_files(root: Path, syscfg_dir: Optional[Path]) -> dict:
    """合并 board.c + solution C 文件的实例发现.

    收集顺序保证稳定 (deterministic): 先 board.c (SysConfig 权威), 再工程根浅层 .c
    (solution 源), 最后深扫. 去重保留首次出现.
    """
    texts = []
    if syscfg_dir and (syscfg_dir / "board.c").is_file():
        try:
            texts.append((syscfg_dir / "board.c").read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
    # solution 工程: 实例直接引用在根目录 .c (buck_hal.c/buck.c/buck_main.c)
    for p in sorted(root.glob("*.c")):
        try:
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
    combined = _scan_bases("\n".join(texts))
    return combined


def extract_sysclk(clocktree: Path) -> Optional[int]:
    """clocktree.h → SYSCLK Hz; 解析失败返回 None."""
    try:
        text = clocktree.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"\bSYSCLK\s*=\s*(\d+)\s*MHz", text)  # \b 排除 PLLSYSCLK (先出现, 可能 != SYSCLK)
    if not m:
        return None
    try:
        return int(m.group(1)) * 1_000_000
    except ValueError:
        return None


def extract_peripherals(root: Path) -> dict:
    """工程根 → periph dict (纯函数, 可单测)."""
    syscfg_dir = find_syscfg_dir(root)
    syscfg_path = syscfg_dir.relative_to(root).as_posix() if syscfg_dir else ""  # 正斜杠, C 字符串/路径消费者安全

    # mcu: main.syscfg 头注释 + clocktree SYSCLK
    mcu = {"device": "", "part": "", "package": "", "sysclk_hz": 100_000_000}  # F28004x 默认
    syscfg_file = root / "main.syscfg"
    if syscfg_file.is_file():
        mcu.update(parse_main_syscfg(syscfg_file))
    if syscfg_dir and (syscfg_dir / "clocktree.h").is_file():
        clk = extract_sysclk(syscfg_dir / "clocktree.h")
        if clk:
            mcu["sysclk_hz"] = clk

    # peripherals: board.c + solution C 合并
    found = _scan_source_files(root, syscfg_dir)

    # epwm 补 timer 映射 (BSP_TIMER_A..F), EPWM7/8 无映射
    epwm = []
    for e in found["epwm"]:
        entry = dict(e)
        entry["timer"] = _EPWM_TO_TIMER.get(e["instance"], "")
        epwm.append(entry)

    return {
        "platform": "c2000",
        "mcu": mcu,
        "peripherals": {
            "epwm": epwm,
            "adc": found["adc"],
            "cla": found["cla"],
        },
        "syscfg": {"dir": syscfg_path},
    }


# ======== 缓存 (对标 ioc_parse) ========

def parse_and_cache(root: Path, cache_path: Path) -> dict:
    """解析 main.syscfg 并缓存到 .coop/periph.yaml. 返回 periph dict."""
    periph = extract_peripherals(root)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        yaml.safe_dump(periph, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return periph


def _cache_is_fresh(root: Path, cache_path: Path) -> bool:
    """缓存有效: main.syscfg + 全部候选源未变."""
    markers = [root / "main.syscfg"]
    if markers[0].is_file():
        base = markers[0].stat().st_mtime_ns
    else:
        return False
    syscfg_dir = find_syscfg_dir(root)
    if syscfg_dir:
        markers += [syscfg_dir / "board.c", syscfg_dir / "clocktree.h"]
    markers += list(root.glob("*.c"))
    for m in markers:
        try:
            if m.stat().st_mtime_ns > base:
                return False
        except OSError:
            continue
    try:
        return cache_path.stat().st_mtime_ns >= base
    except OSError:
        return False


def load_or_parse(root: Path, cache_path: Path, force: bool = False) -> dict:
    """缓存存在且源未变 → 直接读缓存; 否则重解析."""
    if not force and cache_path.is_file() and _cache_is_fresh(root, cache_path):
        try:
            cached = yaml.safe_load(cache_path.read_text(encoding="utf-8"))
            if cached and cached.get("platform") == "c2000":
                return cached
        except Exception:
            pass
    return parse_and_cache(root, cache_path)


def cache_path_for(root: Path) -> Path:
    return root / ".coop" / "periph.yaml"


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    periph = extract_peripherals(root)
    print(yaml.safe_dump(periph, allow_unicode=True, sort_keys=False))
