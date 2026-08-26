"""yamc/cli — 伞命令 `yamc <tool> <action>` 与全部 `yamc_*` 入口（对标 libxr 的 xr_* 九命令 + yamc 衍生命令）。

双拼写单实现：
  pyproject 注册全部 `yamc_<tool>_<action>` 独立入口（如 yamc_ioc_parse、yamc_tune_static）；
  本模块 umbrella_main 提供 `yamc <tool> <action>` 别名（yamc ioc parse ≡ yamc_ioc_parse）。

退出码契约：0=全部 [Pass]；1=任一 [FAIL]；2=平台/参数不支持。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import __version__
from . import engine, params as params_mod, project_probe, topo
from .version import check_and_print as _version_check, local_version

_LOG_TAG = {"info": "INFO", "pass": "Pass", "warn": "WARN", "fail": "FAIL"}


def _log(level: str, msg: str) -> None:
    print(f"[{_LOG_TAG.get(level, level.upper())}] {msg}")


def _setup_logging(argv: Optional[list[str]]) -> None:
    if "--debug" in (argv or []) or os.environ.get("YAMC_DEBUG"):
        logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


def _load_params(path: Optional[str]) -> Optional[dict]:
    """--params YAML → 平铺 dict（config: {power: {...}} 规范化，同 yamc_cfg）。"""
    if not path:
        return None
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"无法读取 --params: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"--params 顶层应为 dict: {path}")
    if "config" in data:
        power = data["config"].get("power") or {}
        if isinstance(power, dict):
            return engine._flatten_dict(power)
    return data


def _set_nested(d: dict, dotted: str, value) -> None:
    """'pid_v.kp' → d['pid_v']['kp'] = value（点分路径嵌入嵌套结构）。"""
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _parse_dotted_sets(items: list[str]) -> dict:
    """['k=v', 'a.b=2'] → {'k': v, 'a.b': 2}（数值/布尔自动转）。"""
    out: dict = {}
    for raw in items:
        key, _, val = raw.partition("=")
        if not key:
            raise ValueError(f"无效 --set 项: {raw}（应为 key=value）")
        s = val.strip()
        try:
            if s.lower() in ("true", "false"):
                v: object = s.lower() == "true"
            elif s.lstrip("+-").isdigit():
                v = int(s)
            else:
                v = float(s)
        except ValueError:
            v = s
        out[key.strip()] = v
    return out


# ═══════════════════════════════════════════════════════════════
#  libxr 九命令对标
# ═══════════════════════════════════════════════════════════════

def cmd_ioc_parse(argv: Optional[list[str]] = None) -> int:
    """对标 xr_parse_ioc: STM32 .ioc → 外设 YAML + 摘要（= 原 yamc_ioc.py 逻辑）。"""
    _setup_logging(argv)
    from . import ioc_parse

    ap = argparse.ArgumentParser(prog="yamc_ioc_parse", description="解析 CubeMX .ioc → 外设 YAML + 摘要")
    ap.add_argument("-d", "--directory", default=".", help=".ioc 所在工程根")
    ap.add_argument("-o", "--output", default=None, help="输出 YAML 路径（默认 .hardc/<stem>.periph.yaml）")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重解析")
    ap.add_argument("--verbose", action="store_true", help="详细日志")
    args = ap.parse_args(argv)

    root = Path(args.directory).resolve()
    ioc = project_probe.find_ioc(root)
    if ioc is None:
        print(f"[FAIL] 未找到 .ioc: {root}", file=sys.stderr)
        return 1
    out = Path(args.output) if args.output else ioc_parse.cache_path_for(root, ioc)
    try:
        periph = ioc_parse.load_or_parse(ioc, out, force=args.force)
    except Exception as exc:
        print(f"[FAIL] 解析 .ioc 失败: {exc}", file=sys.stderr)
        return 1

    mcu = periph.get("mcu") or {}
    print(f"[Pass] 已解析 {ioc.name} → {out}")
    print(f"  平台: {periph.get('platform', '?')}  MCU: {mcu.get('family', '?')} {mcu.get('name', '?')}")
    per = lambda k: len(periph.get("peripherals", {}).get(k, []))
    print(f"  HRTIM: {per('hrtim')}  ADC: {per('adc')}  UART: {per('uart')}  CAN: {per('can')}")
    if args.verbose:
        print("---- periph YAML ----")
        print(yaml.safe_dump(periph, allow_unicode=True, sort_keys=False))
    return 0


def cmd_syscfg_parse(argv: Optional[list[str]] = None) -> int:
    """（xr_parse_ioc 的 C2000 侧）: main.syscfg → C2000 外设 YAML + 摘要。"""
    _setup_logging(argv)
    from . import c2000_syscfg

    ap = argparse.ArgumentParser(prog="yamc_syscfg_parse",
                                 description="C2000 main.syscfg → 外设 YAML + 摘要")
    ap.add_argument("-d", "--directory", default=".", help=".syscfg 所在工程根")
    ap.add_argument("-o", "--output", default=None, help="输出 YAML 路径（默认 .hardc/periph.yaml）")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重解析")
    args = ap.parse_args(argv)

    root = Path(args.directory).resolve()
    out = Path(args.output) if args.output else c2000_syscfg.cache_path_for(root)
    try:
        periph = c2000_syscfg.load_or_parse(root, out, force=args.force)
    except Exception as exc:
        print(f"[FAIL] 解析 main.syscfg 失败: {exc}", file=sys.stderr)
        return 1
    mcu = periph.get("mcu") or {}
    print(f"[Pass] 已解析 {root.name} → {out}")
    print(f"  平台: {periph.get('platform', '?')}  设备: {mcu.get('device', '?')}")
    print(f"  ePWM: {len(periph.get('peripherals', {}).get('epwm', []))}  "
          f"ADC: {len(periph.get('peripherals', {}).get('adc', []))}  "
          f"CLA: {len(periph.get('peripherals', {}).get('cla', []))}")
    if "--verbose" in (argv or []):
        print("---- periph YAML ----")
        print(yaml.safe_dump(periph, allow_unicode=True, sort_keys=False))
    return 0


def cmd_parse(argv: Optional[list[str]] = None) -> int:
    """对标 xr_parse（通用包装器）: 探测平台 → stm32 走 ioc_parse / c2000 走 syscfg_parse。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="yamc_parse", description="平台感知的外设解析包装器")
    ap.add_argument("-d", "--directory", default=".", help="工程根")
    args, rest = ap.parse_known_args(argv)

    root = Path(args.directory).resolve()
    platform = project_probe.detect_platform(root)
    if platform is None:
        print(f"[FAIL] {root} 未识别平台（需 .ioc / main.syscfg）", file=sys.stderr)
        return 2
    if platform == "stm32":
        return cmd_ioc_parse(["-d", str(root), *rest])
    return cmd_syscfg_parse(["-d", str(root), *rest])


def _gen_common(argv: Optional[list[str]], stop_after: int, extra_opts: Optional[dict] = None) -> int:
    _setup_logging(argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--dir", default=".", help="工程根（缺省 cwd 向上探测）")
    ap.add_argument("-t", "--topology", default="buck", help="拓扑名（Config/topologies/<name>.yaml）")
    ap.add_argument("--params", default=None, help="参数 YAML（平铺或 config: {power: {...}}）")
    ap.add_argument("--out", default=None, help="生成相对目录（默认 User/Application）")
    ap.add_argument("--git-source", default=None, help="HardC git 仓库 URL（submodule add）")
    ap.add_argument("--hardc-path", default=None, help="--no-submodule 时 HardC 目录路径")
    ap.add_argument("--no-submodule", action="store_true", help="adopt 已有 HardC 目录")
    ap.add_argument("--no-build", action="store_true", help="跳过构建（stop_after<7 时本即跳过）")
    ap.add_argument("--sdk-dir", default=None, help="c2000: C2000Ware/DigitalPower SDK 根")
    args = ap.parse_args(argv)
    try:
        params = _load_params(args.params)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    opts = dict(extra_opts or {})
    if args.out:
        opts["out_rel"] = args.out
    if args.no_submodule:
        opts["no_submodule"] = True
    if args.hardc_path:
        opts["hardc_path"] = Path(args.hardc_path)
    if args.git_source:
        opts["git_source"] = args.git_source
    if args.no_build:
        opts["no_build"] = True
    if args.sdk_dir:
        opts["sdk_dir"] = args.sdk_dir
    res = engine.run_pipeline(Path(args.dir), args.topology, params,
                              opts=opts, log=_log, stop_after=stop_after)
    return int(res.get("exit_code", 1))


def cmd_gen_code(argv: Optional[list[str]] = None) -> int:
    """对标 xr_gen_code_stm32: 拓扑+外设 → app_main.c/h（stop_after=4, 平台内部分派）。"""
    return _gen_common(argv, stop_after=4)


def cmd_gen(argv: Optional[list[str]] = None) -> int:
    """对标 xr_gen_code（通用包装器）: 同 gen_code。"""
    return cmd_gen_code(argv)


def cmd_gen_bootloader(argv: Optional[list[str]] = None) -> int:
    """衍生: 生成 bootloader_main.c/h（stop_after=5, 强制 bootloader）。"""
    return _gen_common(argv, stop_after=5, extra_opts={"gen_bootloader": True})


def cmd_cmake_inject(argv: Optional[list[str]] = None) -> int:
    """对标 xr_stm32_cmake: CMakeLists 幂等注入 HardC 块（stop_after=6）。"""
    return _gen_common(argv, stop_after=6)


def cmd_cfg_run(argv: Optional[list[str]] = None) -> int:
    """对标 xr_cubemx_cfg: 六步全流水线（stop_after=7）。"""
    _setup_logging(argv)
    ap = argparse.ArgumentParser(prog="yamc_cfg_run",
                                 description="HardC 接入工具链（对标 xr_cubemx_cfg）")
    ap.add_argument("-d", "--dir", default=".", help="外部工程根（含 .ioc/.syscfg）")
    ap.add_argument("-t", "--topology", default="buck", help="拓扑名")
    ap.add_argument("--git-source", default=None,
                    help=f"HardC git 仓库 URL（submodule add; 缺省 {engine.DEFAULT_HARDC_GIT_SOURCE}）")
    ap.add_argument("--hardc-path", default=None, help="--no-submodule 时 HardC 目录路径")
    ap.add_argument("--no-submodule", action="store_true", help="adopt 已有 HardC 目录")
    ap.add_argument("--no-build", action="store_true", help="跳过构建")
    ap.add_argument("--params", default=None, help="参数 YAML")
    ap.add_argument("--sdk-dir", default=None, help="c2000: C2000Ware/DigitalPower SDK 根")
    args = ap.parse_args(argv)
    try:
        params = _load_params(args.params)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    res = engine.run_pipeline(
        Path(args.dir), args.topology, params,
        opts={"no_submodule": args.no_submodule,
              "hardc_path": Path(args.hardc_path) if args.hardc_path else None,
              "git_source": args.git_source,
              "no_build": args.no_build,
              "sdk_dir": args.sdk_dir},
        log=_log,
    )
    return int(res.get("exit_code", 1))


def cmd_flash(argv: Optional[list[str]] = None) -> int:
    """对标 xr_stm32_flash（扩展）: 分区真相 → bsp_flash_map.h + 两个 .ld。"""
    from . import flash_map_gen
    return flash_map_gen.main(argv)


def cmd_switch(argv: Optional[list[str]] = None) -> int:
    """对标 xr_stm32_toolchain_switch: CMakePresets 工具链切换。"""
    from . import yamc_switch
    return yamc_switch.main(argv)


def cmd_cubemx_generate(argv: Optional[list[str]] = None) -> int:
    """对标 xr_cubemx_generate（实现）: stm32 工程自动重生成。"""
    _setup_logging(argv)
    from . import cubemx_generate
    return cubemx_generate.main(argv)


def cmd_ccs_generate(argv: Optional[list[str]] = None) -> int:
    """（xr_cubemx_generate 的 C2000 侧）: c2000 工程自动重生成。"""
    _setup_logging(argv)
    from . import ccs_generate
    return ccs_generate.main(argv)


# ═══════════════════════════════════════════════════════════════
#  yamc 衍生命令
# ═══════════════════════════════════════════════════════════════

def cmd_probe(argv: Optional[list[str]] = None) -> int:
    """探测平台/工程根/库根/工具链。"""
    _setup_logging(argv)
    ap = argparse.ArgumentParser(prog="yamc_probe", description="探测平台/工程根/hardc 库根/工具链")
    ap.add_argument("-d", "--dir", default=".", help="探测起点")
    ap.add_argument("--hardc-path", default=None, help="显式 hardc 库根")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args(argv)

    info = project_probe.probe_all(Path(args.dir), args.hardc_path)
    if args.json:
        import json
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0 if info.get("ok") else 2

    if not info.get("ok"):
        print(f"[FAIL] {info.get('reason', '探测失败')}", file=sys.stderr)
        return 2 if info.get("root") else 1
    print(f"[Info] 工程根: {info.get('root') or '未找到（加 -d <工程根>）'}")
    print(f"[Info] 平台:   {info.get('platform') or '未识别（需 .ioc / main.syscfg）'}")
    print(f"[Pass] .ioc:   {info.get('ioc') or '—'}")
    print(f"[Pass] syscfg: {info.get('syscfg') or '—'}")
    print(f"[Info] hardc:  {info.get('hardc') or '未发现（接入时自动拉取官方 HardC）'}")
    print(f"[Pass] cmake:  {info.get('cmake') or '未找到'}")
    for name, path in (info.get("toolchains") or {}).items():
        print(f"[Info] {name}: {path or '未找到'}")
    return 0


def cmd_check(argv: Optional[list[str]] = None) -> int:
    """自检（对标 PackageInfo.check_and_print）: 版本/库根/工具链/依赖/平台。"""
    _setup_logging(argv)
    if "-h" in (argv or []) or "--help" in (argv or []):
        print("yamc check [--all] — 自检: 版本/工程根/hardc 库根/工具链/依赖")
        return 0
    _version_check()

    fails = 0
    info = project_probe.probe_all(os.getcwd())
    if info.get("root"):
        print(f"[Pass] 工程根: {info['root']}  平台: {info['platform']}")
    else:
        print(f"[WARN] 未从 cwd 探测到工程根（多数命令可用 --root/-d 显式给出）")
    if info.get("hardc"):
        print(f"[Pass] hardc 库根: {info['hardc']}")
    else:
        print(f"[WARN] 未发现本地 hardc — 接入时自动 submodule 拉取官方 HardC（--git-source 可改）")
        fails += 0
    if info.get("cmake"):
        print(f"[Pass] cmake: {info['cmake']}")
    else:
        print(f"[WARN] cmake 未找到（编译功能不可用）")
    if info.get("git"):
        print(f"[Pass] git: {info['git']}")
    else:
        print(f"[WARN] git 未找到（submodule 接入不可用）")
    if info.get("toolchains", {}).get("starm-clang.cmake"):
        print(f"[Pass] starm-clang 工具链存在")
    else:
        print(f"[WARN] 未在库根找到 starm-clang 工具链（stm32 构建会失败）")

    try:
        import yaml as _y  # noqa: F401
        print("[Pass] pyyaml 依赖可用")
    except ImportError:
        print("[FAIL] pyyaml 缺失 — pip install pyyaml")
        fails += 1

    return 1 if fails else 0


def cmd_topo(argv: Optional[list[str]] = None) -> int:
    """拓扑选择: list / show <name> / gen <name>。"""
    _setup_logging(argv)
    ap = argparse.ArgumentParser(prog="yamc_topo", description="拓扑选择（数据源: hardc 库根 Config/topologies/）")
    ap.add_argument("-d", "--dir", default=".", help="起点目录")
    ap.add_argument("--hardc-path", default=None, help="显式 hardc 库根")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出全部拓扑（仅 ready 可生成）")
    p_show = sub.add_parser("show", help="展示拓扑 schema（params 表）")
    p_show.add_argument("name")
    p_gen = sub.add_parser("gen", help="按拓扑生成工程骨架")
    p_gen.add_argument("name")
    p_gen.add_argument("--mcu", default="STM32F334R8", help="MCU（默认 STM32F334R8）")
    p_gen.add_argument("--project", default=None, help="工程名（默认=拓扑名）")
    p_gen.add_argument("--bootloader", action="store_true", help="开启双固件 bootloader")
    p_gen.add_argument("--out", default=None, help="输出目录（默认 build/gen/<name>/）")
    args = ap.parse_args(argv)

    hardc = topo.resolve_hardc(Path(args.dir), args.hardc_path)
    if hardc is None:
        print("[FAIL] 未找到 hardc 库根 — 请先 `yamc cfg_run -d <工程>` 自动接入 HardC"
              "（或设 HARDC_LIB_DIR / --hardc-path）", file=sys.stderr)
        return 1

    if args.command == "list":
        items = topo.list_topologies(hardc)
        if not items:
            print(f"[WARN] {hardc}/Config/topologies/ 无拓扑")
            return 0
        for t in items:
            mark = "" if t["status"] == "ready" else "  (待实现)"
            print(f"  {t['name']:<16} [{t['status']}]{mark}  参数 {t['params_count']}  {t['description']}")
        return 0

    t = topo.show_topology(hardc, args.name)
    if t is None:
        print(f"[FAIL] 拓扑不存在: {args.name}", file=sys.stderr)
        return 1

    if args.command == "show":
        print(f"拓扑: {t['name']}  [{t['status']}]")
        if t["description"]:
            print(f"描述: {t['description']}")
        params = t["data"].get("params") or []
        if params:
            print("参数 schema:")
            for p in params:
                slot = p.get("slot")
                key = str(p.get("key", ""))
                label = str(p.get("label") or key)
                unit = str(p.get("unit") or "")
                default = p.get("default")
                tip = f"{label}  (default={default}{' ' + unit if unit else ''})"
                if slot is not None:
                    tip += f"  slot {slot}"
                print(f"    {key:<16} {tip}")
        return 0

    # gen: 对标 GUI Tab2「生成工程」
    return _topo_gen(hardc, t, args)


def _topo_gen(hardc: Path, t: dict, args: argparse.Namespace) -> int:
    """拓扑 → Config/projects/<name>.yaml + scaffold 骨架 + App 模板物化（+bootloader）。"""
    from . import scaffold
    from .params import find_project_root as _find_static_root

    name = (args.project or "").strip() or t["name"]
    modules = t["data"].get("modules") or []
    if not modules:
        print(f"[FAIL] 拓扑 {t['name']} 缺少 modules 列表", file=sys.stderr)
        return 1
    proj_yaml = {
        "project": name,
        "mcu": args.mcu,
        "description": t["data"].get("description") or f"{t['name']} 工程",
        "modules": modules,
    }
    if args.bootloader:
        bl = t["data"].get("bootloader") or {}
        proj_yaml["bootloader"] = {
            "enable": True,
            "mcu": str(bl.get("mcu") or "").strip() or args.mcu.lower(),
            "up_port": str(bl.get("up_port") or "uart"),
            "flash_dir": str(bl.get("flash_dir") or "."),
        }
    proj_dir = hardc / "Config" / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj_path = proj_dir / f"{name}.yaml"
    proj_path.write_text(yaml.safe_dump(proj_yaml, allow_unicode=True, sort_keys=False,
                                        default_flow_style=False), encoding="utf-8")

    ret = scaffold.main(["gen", proj_path.as_posix(), "--out", args.out] if args.out
                        else ["gen", proj_path.as_posix()])
    if ret != 0:
        print("[FAIL] scaffold 骨架生成失败", file=sys.stderr)
        return ret

    gen_dir = hardc / "build" / "gen" / name
    gen_dir.mkdir(parents=True, exist_ok=True)
    for stem in ("app_main.c", "app_main.h"):
        src = hardc / "App" / f"{stem}.tmpl"
        if src.is_file():
            (gen_dir / stem).write_bytes(src.read_bytes())

    if args.bootloader:
        from . import flash_map_gen
        from .gen_app import gen_bootloader
        bl = proj_yaml["bootloader"]
        fm_ret = flash_map_gen.cmd_gen(hardc, hardc / "Config" / "flash_map.yaml",
                                       bl["mcu"], gen_dir)
        if fm_ret != 0:
            print("[FAIL] flash_map 分区生成失败", file=sys.stderr)
            return fm_ret
        gbl_topo = dict(t["data"])
        gbl_topo["bootloader"] = bl
        gbl = gen_bootloader(gbl_topo, {"platform": "stm32", "peripherals": {"hrtim": [], "adc": [], "can": []}},
                             hardc, gen_dir)
        print(f"[Pass] Bootloader 生成: {gbl['bl_c']} + {gbl['bl_h']}")

    print(f"[Pass] 工程 [{name}] 骨架生成: {gen_dir}")
    print(f"[Pass] Config/projects/{name}.yaml 已写入（MCU={args.mcu}, {len(modules)} 模块）")
    return 0


def cmd_scaffold(argv: Optional[list[str]] = None) -> int:
    from . import scaffold
    return scaffold.main(argv)


def cmd_merge(argv: Optional[list[str]] = None) -> int:
    from . import merge_firmware
    return merge_firmware.main(argv)


def cmd_build(argv: Optional[list[str]] = None) -> int:
    """独立编译（对标 GUI 编译按钮 / engine 第 6 步）。"""
    _setup_logging(argv)
    from .build import build_command, detect_build, find_cmake
    ap = argparse.ArgumentParser(prog="yamc_build", description="编译外部/物化工程（cmake --build）")
    ap.add_argument("-d", "--dir", default=".", help="工程根")
    ap.add_argument("--toolchain", default=None, help="工具链 cmake 文件（stm32/c2000）")
    args = ap.parse_args(argv)

    proj = project_probe.find_project_root(Path(args.dir))
    root = proj if proj is not None else Path(args.dir)
    if args.toolchain:
        ok = engine.run_build(root, _log, Path(args.toolchain))
        return 0 if ok else 1
    info = detect_build(root)
    if info is None:
        print(f"[WARN] 未检测到构建系统（build/CMakeCache.txt）— 试试 --toolchain", file=sys.stderr)
        return 1
    if find_cmake() is None:
        print("[FAIL] cmake 未找到", file=sys.stderr)
        return 1
    cmd = build_command(info)
    print(f"$ {' '.join(cmd)}")
    import subprocess
    r = subprocess.run(cmd, cwd=str(root))
    return r.returncode


# ═══════════════════════════════════════════════════════════════
#  静态调参 / 动态调参
# ═══════════════════════════════════════════════════════════════

def cmd_params(argv: Optional[list[str]] = None) -> int:
    """静态调参数据面: 变体发现 / 变体内容（= GUI Tab「参数注入」数据层）。"""
    _setup_logging(argv)
    ap = argparse.ArgumentParser(prog="yamc_params", description="参数变体发现与展示（静态调参数据层）")

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-d", "--dir", default=".", help="起点目录")

    sub = ap.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="列出变体 + 注入目标 + 当前注入 id")
    _add_common(p_list)
    p_show = sub.add_parser("show", help="展示变体")
    _add_common(p_show)
    p_show.add_argument("id", help="变体 config_id")
    p_show.add_argument("--as-yaml", action="store_true", help="打印原 YAML")
    p_show.add_argument("--as-c", action="store_true", help="打印 C 注入预览")
    args = ap.parse_args(argv)

    ws = params_mod.discover_params(Path(args.dir))
    if not ws["ok"]:
        print(f"[FAIL] {ws['reason']}", file=sys.stderr)
        return 1
    if args.command == "list":
        print(f"[Pass] 工程根: {ws['root']}  布局: {ws['layout']}")
        print(f"[Info] 注入目标: {ws['target_file'].relative_to(ws['root']) if ws['target_file'] else '未找到（先 topo gen 物化）'}")
        print(f"[Info] 当前注入: {ws['current_id'] or '未知'}")
        for cfg in ws["configs"]:
            mark = "*" if cfg["config_id"] == ws["current_id"] else " "
            print(f"  {mark} {cfg['config_id']:<20} {cfg['description']}")
        return 0

    for cfg in ws["configs"]:
        if cfg["config_id"] == args.id:
            if args.as_c:
                print(params_mod.render_config_block(cfg["config"]))
            elif args.as_yaml:
                print(cfg["path"].read_text(encoding="utf-8").rstrip())
            else:
                for dotted, val in params_mod.flatten_config_tree(cfg["config"]):
                    print(f"  {dotted:<24} {val!r}")
            return 0
    print(f"[FAIL] 变体不存在: {args.id}", file=sys.stderr)
    return 1


def cmd_tune_static(argv: Optional[list[str]] = None) -> int:
    """静态调参闭环（= GUI「参数注入」动作的脚本化 twin）。

    --dry-run 缺省: 只打印将注入块（预览）；--apply 注入 C；--save 写回变体 YAML；--build 编译。
    """
    _setup_logging(argv)
    from .build import build_command, detect_build, find_cmake

    ap = argparse.ArgumentParser(prog="yamc_tune_static", description="静态调参（参数 → CONFIG 块注入）")
    ap.add_argument("-d", "--dir", default=".", help="起点目录")
    ap.add_argument("--variant", required=True, help="变体 config_id")
    ap.add_argument("--set", action="append", default=[], metavar="key=value",
                    help="覆盖参数（可多次，点分路径，如 pid_v.kp=2.0）")
    ap.add_argument("--params", default=None, help="覆盖参数 YAML")
    ap.add_argument("--save", action="store_true", help="写回变体 YAML")
    ap.add_argument("--apply", action="store_true", help="注入 C（CONFIG BEGIN/END）")
    ap.add_argument("--build", action="store_true", help="注入后编译")
    ap.add_argument("--dry-run", action="store_true", help="只打印将注入块")
    args = ap.parse_args(argv)

    ws = params_mod.discover_params(Path(args.dir))
    if not ws["ok"]:
        print(f"[FAIL] {ws['reason']}", file=sys.stderr)
        return 1
    cfg = next((c for c in ws["configs"] if c["config_id"] == args.variant), None)
    if cfg is None:
        print(f"[FAIL] 变体不存在: {args.variant}", file=sys.stderr)
        return 1

    merged = dict(cfg["config"])
    try:
        for k, v in _parse_dotted_sets(args.set).items():
            _set_nested(merged, k, v)  # 点分路径嵌入嵌套结构（如 pid_v.kp）
        if args.params:
            overlay = _load_params(args.params) or {}
            merged.update(overlay)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    rendered = params_mod.render_config_block(merged)

    if args.apply:
        target = ws["target_file"]
        if target is None:
            print("[FAIL] 无注入目标（先 yamc_topo gen 物化 app_main.c）", file=sys.stderr)
            return 1
        if ws["current_id"] and ws["current_id"] != args.variant:
            print(f"[WARN] 当前注入 [{ws['current_id']}] ≠ 目标变体 [{args.variant}] — 继续注入（GUI 此处会弹确认）")
        if not params_mod.inject_config(target, rendered, args.variant):
            print(f"[FAIL] 注入失败: {target} 缺 CONFIG 标记", file=sys.stderr)
            return 1
        print(f"[Pass] 已注入 {target} (config={args.variant})")

    if args.save:
        params_mod.write_variant_yaml(cfg["path"], cfg["config_id"], cfg["description"], merged)
        print(f"[Pass] 参数已保存 → {cfg['path']}")

    if args.build:
        info = detect_build(ws["root"])
        if info is None:
            print("[FAIL] 未检测到构建系统（build/CMakeCache.txt）", file=sys.stderr)
            return 1
        if find_cmake() is None:
            print("[FAIL] cmake 未找到", file=sys.stderr)
            return 1
        cmd = build_command(info)
        print(f"$ {' '.join(cmd)}")
        import subprocess
        r = subprocess.run(cmd, cwd=str(ws["root"]))
        if r.returncode != 0:
            return r.returncode

    if not (args.apply or args.save or args.build) or args.dry_run:
        print("---- 将注入 CONFIG 块 ----")
        print(f"/* config: {args.variant} */")
        print(rendered)
    return 0


def cmd_serial(argv: Optional[list[str]] = None) -> int:
    """动态调参: list / tune / watch（0xFB 帧与 GUI Tab3 逐字节一致）。"""
    _setup_logging(argv)
    from . import serial_tune

    ap = argparse.ArgumentParser(prog="yamc_serial", description="动态调参（串口 0xFB 帧）")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="枚举串口")
    p_tune = sub.add_parser("tune", help="构造并下发 0xFB 帧")
    p_tune.add_argument("--port", required=True)
    p_tune.add_argument("--baud", type=int, default=115200)
    p_tune.add_argument("--param", action="append", default=[], metavar="key=value")
    p_tune.add_argument("--params", default=None, help="参数 YAML")
    p_tune.add_argument("-t", "--topology", default=None, help="拓扑名（取 slot 布局）")
    p_tune.add_argument("--hardc-path", default=None)
    p_watch = sub.add_parser("watch", help="接收回显")
    p_watch.add_argument("--port", required=True)
    p_watch.add_argument("--baud", type=int, default=115200)
    p_watch.add_argument("--duration", type=float, default=None)
    args = ap.parse_args(argv)

    try:
        if args.command == "list":
            ports = serial_tune.list_ports()
            if not ports:
                print("[WARN] 未发现串口")
                return 0
            for p in ports:
                print(f"  {p}")
            return 0

        if args.command == "watch":
            serial_tune.watch(args.port, args.baud, args.duration)
            return 0

        # tune
        values = _parse_dotted_sets(args.param)
        if args.params:
            values.update(_load_params(args.params) or {})
        if not values:
            print("[FAIL] 未给出任何 --param key=value", file=sys.stderr)
            return 1
        if not args.topology:
            # 无拓扑 slot 布局 → 全部按默认槽位 0..n 顺序（GUI 场景必须给 -t）
            print("[WARN] 未给 -t 拓扑，无法取 slot 布局 — 请给 -t 或 --params")
            return 1
        hardc = topo.resolve_hardc(None, Path(args.hardc_path) if args.hardc_path else None)
        if hardc is None:
            print("[FAIL] 未找到 hardc 库根", file=sys.stderr)
            return 1
        t = topo.show_topology(hardc, args.topology)
        if t is None:
            print(f"[FAIL] 拓扑不存在: {args.topology}", file=sys.stderr)
            return 1
        slots = serial_tune.slots_from_params(t["data"].get("params") or [], values)
        if not slots:
            print("[FAIL] 拓扑 params schema 无匹配 slot 的参数", file=sys.stderr)
            return 1
        frame = serial_tune.build_frame(slots)
        serial_tune.send_tune(args.port, frame, args.baud)
        nz = sum(1 for v in slots.values() if v != 0.0)
        print(f"[Pass] 已下发 48 字节 0xFB 帧（{nz} 个非零系数）→ {args.port} @ {args.baud}")
        return 0
    except serial_tune.SerialTuneError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2 if "pyserial" in str(exc) else 1
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


def cmd_gui(argv: Optional[list[str]] = None) -> int:
    """启动 PyQt6 GUI。"""
    if "-h" in (argv or []) or "--help" in (argv or []):
        print("yamc gui — 启动 PyQt6 四 Tab GUI")
        return 0
    try:
        import PyQt6  # noqa: F401
    except ImportError:
        print("[FAIL] 需要 PyQt6: pip install 'yamc[gui]'", file=sys.stderr)
        return 2
    from . import yaml_config_builder
    return yaml_config_builder._build_gui()


# ═══════════════════════════════════════════════════════════════
#  伞命令
# ═══════════════════════════════════════════════════════════════

_TOOL_TABLE: dict[str, Callable[[Optional[list[str]]], int]] = {
    "cfg_run": cmd_cfg_run,
    "cfg": cmd_cfg_run,
    "parse": cmd_parse,
    "ioc_parse": cmd_ioc_parse,
    "syscfg_parse": cmd_syscfg_parse,
    "gen": cmd_gen,
    "gen_code": cmd_gen_code,
    "gen_bootloader": cmd_gen_bootloader,
    "cmake_inject": cmd_cmake_inject,
    "flash": cmd_flash,
    "switch": cmd_switch,
    "cubemx_generate": cmd_cubemx_generate,
    "ccs_generate": cmd_ccs_generate,
    "probe": cmd_probe,
    "check": cmd_check,
    "topo": cmd_topo,
    "scaffold": cmd_scaffold,
    "merge": cmd_merge,
    "build": cmd_build,
    "params": cmd_params,
    "tune_static": cmd_tune_static,
    "serial": cmd_serial,
    "gui": cmd_gui,
}


def _usage() -> str:
    lines = [
        f"yamc {__version__} — HardC 配置与接入工具链",
        "",
        "用法: yamc <tool> <action> [选项]  或  yamc_<tool>_<action> [选项]",
        "",
        "libxr 九命令对标:",
        "  ioc_parse / syscfg_parse / parse            .ioc|main.syscfg → 外设 YAML",
        "  gen_code / gen / gen_bootloader             拓扑+外设 → app_main.c/h（+bootloader）",
        "  cmake_inject                                 CMakeLists 幂等注入 HardC 块",
        "  flash list|show|gen                         分区真相 → bsp_flash_map.h + .ld",
        "  switch gcc|clang [-g|-n|-p]                 工具链切换",
        "  cfg_run                                      六步全流水线（xr_cubemx_cfg）",
        "  cubemx_generate / ccs_generate              stm32 / c2000 工程自动重生成",
        "",
        "yamc 衍生:",
        "  probe / check                                探测平台 / 自检",
        "  topo list|show|gen <name>                    拓扑选择",
        "  scaffold scan|deps|gen / merge merge|info|selftest",
        "  build [-d]                                   独立编译",
        "  params list|show <id>                        变体发现/展示（静态调参数据层）",
        "  tune_static --variant <id> [--apply|--save|--build|--dry-run]  静态调参",
        "  serial list|tune|watch                       动态调参（0xFB 帧）",
        "  gui                                          启动 PyQt6 GUI",
        "",
        "全局: --debug  退出码: 0=全 Pass / 1=FAIL / 2=平台或依赖不支持",
    ]
    return "\n".join(lines)


def umbrella_main(argv: Optional[list[str]] = None) -> int:
    """`yamc` 伞命令：多词工具名（空格分隔）拼成 snake_case 路由到同 handler。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(_usage())
        return 0
    head = argv[0]
    if head in ("-h", "--help"):
        print(_usage())
        return 0
    if head in ("-v", "--version"):
        print(f"yamc {local_version()}")
        return 0

    for i in range(len(argv), 0, -1):
        key = "_".join(argv[:i])
        handler = _TOOL_TABLE.get(key)
        if handler is not None:
            return handler(argv[i:])
    print(f"[FAIL] 未知工具: {head}（yamc --help 看用法）", file=sys.stderr)
    return 2


def main() -> None:
    sys.exit(umbrella_main())


if __name__ == "__main__":
    main()