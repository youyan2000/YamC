#!/usr/bin/env python3
"""ymac_cfg — 对标 xr_cubemx_cfg 的 HardC 接入工具链 (CLI).

在 STM32CubeMX / C2000 工程根执行一条命令, 编排逻辑见 engine.run_pipeline:
  1. 探测平台 (.ioc=stm32 / main.syscfg=c2000)
  2. HardC git submodule 接入 (或 --no-submodule adopt 已有目录)
  3. 解析 .ioc → 外设 YAML (.hardc/periph.yaml)
  4. 拓扑 + 外设 → 生成 app_main.c/h (完整外设绑定)
  5. CMakeLists.txt 幂等集成 (YmaC HardC BEGIN/END 块)
  6. 编译 (除非 --no-build)

用法:
  ymac_cfg -d <工程根> --topology buck [--git-source <repo-url>]
  ymac_cfg -d <工程根> --topology buck --no-submodule --no-build

退出码: 0=全部 [Pass]; 1=任一 [FAIL]; 2=平台/参数不支持.
GUI (yaml_config_builder.py Tab2) 与 CLI 共用 engine.run_pipeline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml

from engine import _flatten_dict, run_pipeline


def _info(msg: str) -> None:
    print(f"[INFO] {msg}")


def _pass(msg: str) -> None:
    print(f"[Pass] {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


_PRINTERS = {"info": _info, "pass": _pass, "warn": _warn, "fail": _fail}


def _log(level: str, msg: str) -> None:
    _PRINTERS.get(level, _info)(msg)


def load_params(path: Optional[Path]) -> Optional[dict]:
    """--params YAML: 平铺 {'vref': 12.5, 'pid_v.kp': 2} 或 config: {power: {...}}.

    坏路径/坏格式抛 ValueError (main 转 [FAIL] + exit 1), 与 exit 契约
    '0=全部 Pass' 一致 — 用户显式给的 --params 读不了就是失败, 不能静默跑默认值."""
    if not path:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        raise ValueError(f"无法读取 --params: {path}")
    except yaml.YAMLError as exc:
        raise ValueError(f"--params YAML 格式错误: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"--params 顶层应为 dict: {path}")
    if "config" in data:
        power = data["config"].get("power") or {}
        if isinstance(power, dict):
            return _flatten_dict(power)
    return data


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="HardC 接入工具链 (对标 xr_cubemx_cfg)")
    ap.add_argument("-d", "--dir", default=".", help="外部工程根 (含 .ioc/.syscfg)")
    ap.add_argument("--topology", default="buck", help="拓扑名 (Config/topologies/<name>.yaml)")
    ap.add_argument("--git-source", default=None, help="HardC git 仓库 URL (submodule add)")
    ap.add_argument("--hardc-path", default=None, help="--no-submodule 时 HardC 目录路径")
    ap.add_argument("--no-submodule", action="store_true", help="adopt 已有 HardC 目录, 不做 submodule")
    ap.add_argument("--no-build", action="store_true", help="跳过构建")
    ap.add_argument("--params", default=None, help="参数 YAML (平铺或 config: {power: {...}})")
    ap.add_argument("--sdk-dir", default=None, help="c2000: C2000Ware/DigitalPower SDK 根 (缺省自动探测)")
    args = ap.parse_args(argv)

    try:
        params = load_params(Path(args.params)) if args.params else None
    except ValueError as exc:
        _fail(str(exc))
        return 1

    res = run_pipeline(
        Path(args.dir),
        args.topology,
        params=params,
        opts={
            "no_submodule": args.no_submodule,
            "hardc_path": Path(args.hardc_path) if args.hardc_path else None,
            "git_source": args.git_source,
            "no_build": args.no_build,
            "sdk_dir": args.sdk_dir,
        },
        log=_log,
    )
    return res["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
