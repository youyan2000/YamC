"""ymac_cfg — CLI/GUI 共享接入流水线 (纯编排, 无 GUI 依赖).

对标 xr_cubemx_cfg: 一条流水线在外部工程根完成
  探测平台 → HardC submodule 接入 → .ioc 解析 → 生成 app_main.c/h
  → CMake 幂等集成 → 构建.

CLI (ymac_cfg.py) 与 GUI (yaml_config_builder.py Tab2) 共用本模块;
慢操作 (submodule/git/构建) 由 GUI 侧放 QThread, engine 只编排 + 日志回调.
失败不 raise 不 sys.exit, 返回 {ok, reason, exit_code} 由调用方呈现.

日志回调: log(level, msg), level ∈ info|pass|warn|fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from cmake_integrate import compute_devices, inject_cmake_integration, series_from_family
from gen_app import gen_app, load_topology
from ioc_parse import cache_path_for, load_or_parse
from project_probe import detect_platform, find_ioc, find_project_root
from c2000_syscfg import cache_path_for as c2_cache_path_for, load_or_parse as c2_load_or_parse

LogFn = Callable[[str, str], None]

DEFAULT_HARDC_REL = "Middlewares/Third_Party/HardC"

# 默认日志前缀 (CLI 传自己的 _log, 此处仅兜底打印用, 大小写与 CLI 一致)
_LEVEL_TAG = {"info": "INFO", "pass": "Pass", "warn": "WARN", "fail": "FAIL"}


class PipelineError(Exception):
    """流水线业务失败, 携带用户可见 reason 与 CLI 退出码."""

    def __init__(self, reason: str, exit_code: int = 1) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def _flatten_dict(d: dict, prefix: str = "") -> dict:
    """{'pid_v': {'kp': 2}} → {'pid_v.kp': 2} (叶为标量)."""
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten_dict(v, key))
        else:
            out[key] = v
    return out


def normalize_params(params: Optional[dict]) -> Optional[dict]:
    """统一平铺: GUI 表单产出 {power: {...}}, CLI 已是平铺 → 同构.

    gen_app 消费的是带点号的平铺键 ('vref', 'pid_v.kp'), 两种来源都归一化."""
    if params and isinstance(params.get("power"), dict):
        return _flatten_dict(params["power"])
    return params


# ======== git 工具 ========

def _git(root: Path, log: LogFn, *args: str) -> bool:
    try:
        r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                           timeout=120)
        if r.returncode != 0:
            log("warn", f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log("warn", f"git {' '.join(args)} 超时 (120s)")
        return False
    except FileNotFoundError:
        log("warn", "git 不在 PATH")
        return False


# ======== HardC 接入 (git submodule) ========

def ensure_hardc_dir(root: Path, log: LogFn, no_submodule: bool = False,
                    hardc_path: Optional[Path] = None,
                    git_source: Optional[str] = None) -> Path:
    """返回 HardC 目录 (绝对). 无 remote 时 --no-submodule 或 adopt 已有目录."""
    if no_submodule:
        if hardc_path is not None:
            # 显式给出 → 不静默回退 (拼错路径应立即暴露)
            hardc = hardc_path.resolve()
            if not (hardc / "cmake" / "HardC.CMake").is_file():
                raise PipelineError(f"--no-submodule 但 {hardc} 不是 HardC (缺 cmake/HardC.CMake)")
            return hardc
        # 未显式给 --hardc-path: 先试工程内 Middlewares 路径, 再默认 adopt 工具链自身仓库
        # (工具链随库: YmaC/ 的上两级 = HardC 根). 守卫校验 HardC.CMake 存在.
        hardc = (root / DEFAULT_HARDC_REL).resolve()
        if not (hardc / "cmake" / "HardC.CMake").is_file():
            toolchain_root = Path(__file__).resolve().parent.parent
            if (toolchain_root / "cmake" / "HardC.CMake").is_file():
                log("warn", f"{hardc} 不是 HardC, 改用工具链自身仓库 {toolchain_root}")
                hardc = toolchain_root
            else:
                raise PipelineError(f"--no-submodule 但 {hardc} 不是 HardC (缺 cmake/HardC.CMake)")
        return hardc

    hardc = root / DEFAULT_HARDC_REL
    gmodules = root / ".gitmodules"

    if hardc.is_dir():
        log("pass", f"HardC 已存在: {hardc}")
        return hardc.resolve()

    if not (root / ".git").exists():
        log("info", "工程非 git 仓库 → git init")
        _git(root, log, "init")

    # submodule 声明存在但目录缺失 → update
    if gmodules.is_file() and "HardC" in gmodules.read_text(encoding="utf-8", errors="replace"):
        log("info", "submodule 已声明 → git submodule update --init")
        if _git(root, log, "submodule", "update", "--init", DEFAULT_HARDC_REL):
            return hardc.resolve()

    if not git_source:
        raise PipelineError("HardC 目录缺失: 请提供 HardC 仓库 URL 接入, 或跳过 submodule adopt 已有目录")

    log("info", f"git submodule add {git_source}")
    if not _git(root, log, "submodule", "add", git_source, DEFAULT_HARDC_REL):
        raise PipelineError("submodule add 失败")
    return hardc.resolve()


# ======== 构建 ========

def run_build(root: Path, log: LogFn, toolchain_file: Optional[Path] = None) -> bool:
    if not (root / "CMakeLists.txt").is_file():
        log("warn", "无 CMakeLists.txt, 跳过构建")
        return True
    if shutil.which("cmake") is None:
        log("warn", "cmake 不在 PATH, 跳过构建")
        return True
    build_dir = root / "build"
    cfg = ["cmake", "-S", str(root), "-B", str(build_dir)]
    if toolchain_file:
        cfg += ["-DCMAKE_TOOLCHAIN_FILE=" + str(toolchain_file)]
    try:
        r = subprocess.run(cfg, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log("fail", f"cmake configure 失败:\n{r.stderr[-1500:]}")
            return False
        r = subprocess.run(["cmake", "--build", str(build_dir)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log("fail", f"cmake build 失败:\n{r.stderr[-1500:]}")
            return False
        return True
    except FileNotFoundError:
        log("warn", "cmake 不可用, 跳过构建")
        return True
    except subprocess.TimeoutExpired:
        log("fail", "构建超时 (600s)")
        return False


# ======== C2000 SDK 探测 (C2000_SDK_DIR) ========

def _probe_c2000_sdk(root: Path, log: LogFn) -> Optional[str]:
    """定位 C2000Ware/DigitalPower SDK 根 (含 driverlib/f28004x).

    优先级: 环境变量 (C2000WARE/TI_C2000WARE) → 常见默认安装位置.
    返回 None 时调用方应提示 --sdk-dir 显式给出.
    """
    import os
    for env in ("C2000WARE", "TI_C2000WARE", "C2000_SDK_DIR"):
        v = os.environ.get(env)
        if v and (Path(v) / "driverlib" / "f28004x").is_dir():
            log("pass", f"C2000_SDK_DIR 来自环境变量 {env}")
            return v
    for cand in sorted(Path("E:/TIIDE/C2000").glob("*/c2000ware")):
        if (cand / "driverlib" / "f28004x" / "driverlib").is_dir():
            log("pass", f"C2000_SDK_DIR 探测: {cand}")
            return str(cand)
    log("warn", "未探测到 C2000Ware SDK — 用 --sdk-dir 显式指定")
    return None


# ======== 流水线编排 ========

def run_pipeline(start: Path, topology: str, params: Optional[dict] = None,
                 opts: Optional[dict] = None, log: Optional[LogFn] = None) -> dict:
    """在外部工程根执行完整接入流水线.

    start:   探测起点目录 (CLI -d / GUI 目录选择器)
    topology: 拓扑名 (Config/topologies/<name>.yaml)
    params:  参数 dict 覆盖拓扑默认; 平铺 {'vref': 12.5, 'pid_v.kp': 2.0} 或
             嵌套 {'power': {...}} (GUI 表单) 均可, 内部归一化平铺
    opts:   {no_submodule, hardc_path, git_source, no_build, out_rel}
    log:    日志回调 (level, msg); 默认打印 [LEVEL] msg

    返回: {ok, exit_code, reason?, root, platform, hardc, app_c, config_id,
           cmake_inserted, cmake_old_integration, built}
    """
    logger: LogFn = log or (lambda level, msg: print(f"[{_LEVEL_TAG.get(level, level.upper())}] {msg}"))
    opts = opts or {}
    no_submodule = bool(opts.get("no_submodule", False))
    hardc_path = opts.get("hardc_path")
    git_source = opts.get("git_source")
    no_build = bool(opts.get("no_build", False))
    out_rel = str(opts.get("out_rel", "User/Application"))

    out: dict = {"ok": False, "exit_code": 1}
    try:
        # 1. 探测
        logger("info", f"拓扑: {topology}")
        root = find_project_root(start)
        if root is None:
            raise PipelineError(f"未找到工程根 (从 {start} 向上找 .ioc/.syscfg)")
        logger("pass", f"工程根: {root}")
        platform = detect_platform(root)
        if platform is None:
            raise PipelineError(f"{root} 未识别平台 (需 .ioc / main.syscfg / .syscfg)", exit_code=2)
        logger("pass", f"平台: {platform}")
        out["platform"] = platform

        # 2. HardC 接入
        logger("info", "接入 HardC (git submodule)")
        hardc = ensure_hardc_dir(root, logger, no_submodule, hardc_path, git_source)
        logger("pass", f"HardC: {hardc}")
        out["hardc"] = str(hardc)

        # 3. 外设解析 (平台分派)
        sdk_dir = None
        if platform == "stm32":
            ioc = find_ioc(root)
            if ioc is None:
                raise PipelineError(f"{root} 下未找到 .ioc")
            rel = ioc.relative_to(root) if ioc.is_relative_to(root) else ioc
            logger("pass", f".ioc: {rel}")
            periph = load_or_parse(ioc, cache_path_for(root, ioc))
            logger("pass", f"外设解析: {len(periph['peripherals']['hrtim'])} HRTIM, "
                           f"{len(periph['peripherals']['adc'])} ADC, "
                           f"{len(periph['peripherals']['uart'])} UART")
        elif platform == "c2000":
            periph = c2_load_or_parse(root, c2_cache_path_for(root))
            logger("pass", f"外设解析: {len(periph['peripherals']['epwm'])} ePWM, "
                           f"{len(periph['peripherals']['adc'])} ADC, "
                           f"syscfg={periph['syscfg']['dir'] or '未发现'}")
            sdk_dir = opts.get("sdk_dir") or _probe_c2000_sdk(root, logger)
        else:
            raise PipelineError(f"不支持平台: {platform}", exit_code=2)

        # 4. 拓扑 + 生成
        try:
            topo = load_topology(hardc, topology)
        except FileNotFoundError:
            raise PipelineError(f"拓扑不存在: Config/topologies/{topology}.yaml (在 {hardc})")
        if topo.get("status") != "ready":
            logger("warn", f"拓扑 {topology} 状态为 {topo.get('status')}, 继续生成")
        out_dir = root / out_rel
        logger("info", f"生成 app_main.c/h → {out_dir.relative_to(root)}")
        result = gen_app(topo, periph, normalize_params(params), hardc, out_dir)
        logger("pass", f"app_main.c 生成 (config_id={result['config_id']})")
        out["app_c"] = str(result["app_c"])
        out["config_id"] = result["config_id"]

        # 5. CMake 集成 (平台分派: st → HARDC_STM32_SERIES; c2000 → C2000_SDK_DIR)
        cm = root / "CMakeLists.txt"
        if not cm.is_file():
            raise PipelineError(f"未找到 {cm} — 无法集成构建")
        devices = compute_devices(topo)
        extra = ""
        if platform == "stm32":
            series = series_from_family(periph["mcu"].get("family", ""))
            r = inject_cmake_integration(cm, DEFAULT_HARDC_REL, "st", devices,
                                         f"{out_rel}/app_main.c", series, None)
            extra = f"series={series}"
        else:  # c2000
            r = inject_cmake_integration(cm, DEFAULT_HARDC_REL, "c2000", devices,
                                         f"{out_rel}/app_main.c", None, sdk_dir)
            extra = f"sdk_dir={'给出' if sdk_dir else '缺失(构建会失败, 用 --sdk-dir)'}"
        out["cmake_inserted"] = r["inserted"]
        out["cmake_old_integration"] = r["old_integration"]
        if r["old_integration"]:
            logger("warn", "检测到旧版 User/Components... 手工集成, 请手工移除后重跑")
        logger("pass", f"CMake 集成: {'插入' if r['inserted'] else '更新'} HardC 块 ({extra})")

        # 6. 构建
        if no_build:
            logger("pass", "构建跳过 (--no-build)")
            out["built"] = False
        elif platform == "c2000":
            toolchain = hardc / "cmake" / "c2000-ti-cgt.cmake"
            logger("info", f"C2000 构建: cl2000 工具链 {toolchain.relative_to(hardc)}")
            if run_build(root, logger, toolchain):
                logger("pass", "构建通过")
                out["built"] = True
            else:
                raise PipelineError("构建失败")
        elif run_build(root, logger):
            logger("pass", "构建通过")
            out["built"] = True
        else:
            raise PipelineError("构建失败")

        logger("pass", "完成 — app_main.c/h + CMake 接入就绪")
        out["ok"] = True
        out["exit_code"] = 0
        return out
    except PipelineError as exc:
        logger("fail", exc.reason)
        out["ok"] = False
        out["exit_code"] = exc.exit_code
        out["reason"] = exc.reason
        return out
    except Exception as exc:
        # 兜底: 拼错拓扑名/坏 --params 等抛非 PipelineError 的异常时,
        # 也必须干净 [FAIL] (GUI QThread 下裸 traceback = 直接崩溃).
        logger("fail", f"内部错误: {exc}")
        out["ok"] = False
        out["exit_code"] = 1
        out["reason"] = str(exc)
        return out
