#!/usr/bin/env python3
"""ymac_ioc — 对标 xr_parse_ioc: 解析 CubeMX .ioc → 外设 YAML + 控制台摘要 (CLI).

从 STM32CubeMX .ioc 提取外设/引脚/DMA/MCU 信息, 落到 .hardc/<stem>.periph.yaml
(或 -o 指定), 并在终端输出可读摘要 (MCU、HRTIM/ADC/UART/CAN 外设计数等)。

用法:
  ymac_ioc -d <工程根> [-o <out.yaml>] [--verbose]

等价 libxr:
  xr_parse_ioc -d DIRECTORY [-o OUTPUT] [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from ioc_parse import cache_path_for, load_or_parse
from project_probe import find_ioc


def _summary(periph: dict) -> str:
    """控制台摘要: MCU + 各外设计数, 对齐 xr_parse_ioc 的信息量."""
    mcu = periph.get("mcu") or {}
    lines = [
        f"平台: {periph.get('platform', '?')}",
        f"MCU: {mcu.get('family', '?')} {mcu.get('name', '?')} "
        f"(subfamily={mcu.get('subfamily', '?')}, HCLK={mcu.get('hclk_hz', 0)} Hz)",
        f"HRTIM: {len(periph.get('peripherals', {}).get('hrtim', []))}",
        f"ADC:   {len(periph.get('peripherals', {}).get('adc', []))}",
        f"UART:  {len(periph.get('peripherals', {}).get('uart', []))}",
        f"CAN:   {len(periph.get('peripherals', {}).get('can', []))}",
    ]
    return "\n".join("  " + l for l in lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ymac_ioc", description="解析 CubeMX .ioc → 外设 YAML + 摘要")
    ap.add_argument("-d", "--directory", default=".", help=".ioc 所在工程根")
    ap.add_argument("-o", "--output", default=None, help="输出 YAML 路径 (默认 .hardc/<stem>.periph.yaml)")
    ap.add_argument("--verbose", action="store_true", help="详细日志")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重解析")
    args = ap.parse_args(argv)

    root = Path(args.directory)
    ioc = find_ioc(root)
    if ioc is None:
        print(f"[FAIL] 未找到 .ioc: {root}", file=sys.stderr)
        return 1

    out = Path(args.output) if args.output else cache_path_for(root.resolve(), ioc)
    try:
        periph = load_or_parse(ioc, out, force=args.force)
    except Exception as exc:
        print(f"[FAIL] 解析 .ioc 失败: {exc}", file=sys.stderr)
        return 1

    print(f"[Pass] 已解析 {ioc.name} → {out}")
    print(_summary(periph))
    if args.verbose:
        import yaml as _y
        print("---- periph YAML ----")
        print(_y.safe_dump(periph, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
