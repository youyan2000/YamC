"""ymac_cfg — CLI/GUI 共享接入流水线 (纯编排, 无 GUI 依赖).

对标 xr_cubemx_cfg: 一条流水线在外部工程根完成
  探测平台 → C-OOP submodule 接入 → .ioc 解析 → 生成 app_main.c/h
  → CMake 幂等集成 → 构建.

CLI (ymac_cfg.py) 与 GUI (yaml_config_builder.py Tab2) 共用本模块;
慢操作 (submodule/git/构建) 由 GUI 侧放 QThread, engine 只编排 + 日志回调.
失败不 raise 不 sys.exit, 返回 {ok, reason, exit_code} 由调用方呈现.

日志回调: log(level, msg), level ∈ info|pass|warn|fail.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from cmake_integrate import compute_devices, inject_cmake_integration, series_from_family
from gen_app import gen_app, load_topology
from ioc_parse import cache_path_for, load_or_parse
from project_probe import detect_platform, find_ioc, find_project_root

LogFn = Callable[[str, str], None]

DEFAULT_COOP_REL = "Middlewares/Third_Party/C-OOP"


class PipelineError(Exception):
    """流水线业务失败, 携带用户可见 reason 与 CLI 退出码."""

    def __init__(self, reason: str, exit_code: int = 1) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


# ======== git 工具 ========

def _git(root: Path, log: LogFn, *args: str) -> bool:
    try:
        r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
        if r.returncode != 0:
            log("warn", f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
            return False
        return True
    except FileNotFoundError:
        log("warn", "git 不在 PATH")
        return False


# ======== C-OOP 接入 (git submodule) ========

def ensure_coop_dir(root: Path, log: LogFn, no_submodule: bool = False,
                    coop_path: Optional[Path] = None,
                    git_source: Optional[str] = None) -> Path:
    """返回 C-OOP 目录 (绝对). 无 remote 时 --no-submodule 或 adopt 已有目录."""
    if no_submodule:
        coop = (coop_path or root / DEFAULT_COOP_REL).resolve()
        if not (coop / "cmake" / "C-OOP.CMake").is_file():
            raise PipelineError(f"--no-submodule 但 {coop} 不是 C-OOP (缺 cmake/C-OOP.CMake)")
        return coop

    coop = root / DEFAULT_COOP_REL
    gmodules = root / ".gitmodules"

    if coop.is_dir():
        log("pass", f"C-OOP 已存在: {coop}")
        return coop.resolve()

    if not (root / ".git").exists():
        log("info", "工程非 git 仓库 → git init")
        _git(root, log, "init")

    # submodule 声明存在但目录缺失 → update
    if gmodules.is_file() and "C-OOP" in gmodules.read_text(encoding="utf-8", errors="replace"):
        log("info", "submodule 已声明 → git submodule update --init")
        if _git(root, log, "submodule", "update", "--init", DEFAULT_COOP_REL):
            return coop.resolve()

    if not git_source:
        raise PipelineError("C-OOP 目录缺失: 请提供 --git-source <repo-url> 或用 --no-submodule adopt 已有目录")

    log("info", f"git submodule add {git_source}")
    if not _git(root, log, "submodule", "add", git_source, DEFAULT_COOP_REL):
        raise PipelineError("submodule add 失败")
    return coop.resolve()


# ======== 构建 ========

def run_build(root: Path, log: LogFn) -> bool:
    if not (root / "CMakeLists.txt").is_file():
        log("warn", "无 CMakeLists.txt, 跳过构建")
        return True
    if shutil.which("cmake") is None:
        log("warn", "cmake 不在 PATH, 跳过构建")
        return True
    build_dir = root / "build"
    try:
        r = subprocess.run(["cmake", "-S", str(root), "-B", str(build_dir)],
                           capture_output=True, text=True, timeout=600)
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


# ======== 流水线编排 ========

def run_pipeline(start: Path, topology: str, params: Optional[dict] = None,
                 opts: Optional[dict] = None, log: Optional[LogFn] = None) -> dict:
    """在外部工程根执行完整接入流水线.

    start:   探测起点目录 (CLI -d / GUI 目录选择器)
    topology: 拓扑名 (Config/topologies/<name>.yaml)
    params:  平铺参数 dict {'vref': 12.5, 'pid_v.kp': 2.0}, 覆盖拓扑默认
    opts:   {no_submodule, coop_path, git_source, no_build, out_rel}
    log:    日志回调 (level, msg); 默认打印 [LEVEL] msg

    返回: {ok, exit_code, reason?, root, platform, coop, app_c, config_id,
           cmake_inserted, cmake_old_integration, built}
    """
    logger: LogFn = log or (lambda level, msg: print(f"[{level.upper()}] {msg}"))
    opts = opts or {}
    no_submodule = bool(opts.get("no_submodule", False))
    coop_path = opts.get("coop_path")
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

        if platform != "stm32":
            raise PipelineError(f"{platform} 平台接入在 Phase 3 (本次仅支持 stm32)", exit_code=2)
        out["platform"] = platform

        # 2. C-OOP 接入
        logger("info", "接入 C-OOP (git submodule)")
        coop = ensure_coop_dir(root, logger, no_submodule, coop_path, git_source)
        logger("pass", f"C-OOP: {coop}")
        out["coop"] = str(coop)

        # 3. 解析 .ioc
        ioc = find_ioc(root)
        if ioc is None:
            raise PipelineError(f"{root} 下未找到 .ioc")
        rel = ioc.relative_to(root) if ioc.is_relative_to(root) else ioc
        logger("pass", f".ioc: {rel}")
        periph = load_or_parse(ioc, cache_path_for(root, ioc))
        logger("pass", f"外设解析: {len(periph['peripherals']['hrtim'])} HRTIM, "
                       f"{len(periph['peripherals']['adc'])} ADC, "
                       f"{len(periph['peripherals']['uart'])} UART")

        # 4. 拓扑 + 生成
        topo = load_topology(coop, topology)
        if topo.get("status") != "ready":
            logger("warn", f"拓扑 {topology} 状态为 {topo.get('status')}, 继续生成")
        out_dir = root / out_rel
        logger("info", f"生成 app_main.c/h → {out_dir.relative_to(root)}")
        result = gen_app(topo, periph, params, coop, out_dir)
        logger("pass", f"app_main.c 生成 (config_id={result['config_id']})")
        out["app_c"] = str(result["app_c"])
        out["config_id"] = result["config_id"]

        # 5. CMake 集成
        cm = root / "CMakeLists.txt"
        if not cm.is_file():
            raise PipelineError(f"未找到 {cm} — 无法集成构建")
        devices = compute_devices(topo)
        series = series_from_family(periph["mcu"].get("family", ""))
        r = inject_cmake_integration(cm, DEFAULT_COOP_REL, "st", devices,
                                     f"{out_rel}/app_main.c", series)
        out["cmake_inserted"] = r["inserted"]
        out["cmake_old_integration"] = r["old_integration"]
        if r["old_integration"]:
            logger("warn", "检测到旧版 User/Components... 手工集成, 请手工移除后重跑")
        logger("pass", f"CMake 集成: {'插入' if r['inserted'] else '更新'} C-OOP 块 (series={series})")

        # 6. 构建
        if no_build:
            logger("pass", "构建跳过 (--no-build)")
            out["built"] = False
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
