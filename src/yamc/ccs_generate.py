"""yamc/ccs_generate — c2000 工程自动重生成（xr_cubemx_generate 的 C2000 侧）。

定义:「已有工程 → 自动重生成代码」——对 main.syscfg 调 SysConfig CLI 重新生成
外设产物（board.c/board.h 等），校验 syscfg 输出目录，可选 CCS 无头构建。

路径来源: --syscfg-cmd / env SYSCFG_CMD / 常见安装位置自动探测；
         --ccs-path / env CCS_PATH 用于可选 CCS 构建。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

SYSCFG_OUT_CANDIDATES = ("RELEASE/syscfg", "Debug/syscfg", "syscfg")


def _find_sysconfig_cmd(explicit: str = "") -> Optional[str]:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    env = os.environ.get("SYSCFG_CMD")
    if env and Path(env).is_file():
        return env
    found = shutil.which("sysconfig")
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        import glob as _g
        for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            cands = [
                f"{drive}:\\ti\\ccs*/ccs/utils/sysconfig*/sysconfig_cli.bat",
                f"{drive}:\\tools\\sysconfig*/sysconfig_cli.bat",
            ]
            for c in cands:
                candidates.extend(_g.glob(c))
    else:
        candidates = ["/opt/ti/sysconfig/sysconfig_cli.sh", "/usr/local/ti/sysconfig/sysconfig_cli.sh"]
    for c in candidates:
        if Path(c).is_file():
            return str(c)
    return None


def _find_ccs_eclipsec(explicit: str = "") -> Optional[str]:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        if p.is_dir() and (p / "eclipsec.exe").is_file():
            return str(p / "eclipsec.exe")
        return None
    env = os.environ.get("CCS_PATH")
    if env:
        p = Path(env)
        if (p / "eclipsec.exe").is_file():
            return str(p / "eclipsec.exe")
    if sys.platform == "win32":
        import glob as _g
        for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            matches = _g.glob(f"{drive}:\\ti\\ccs*/ccs/eclipse/eclipsec.exe")
            if matches:
                return matches[0]
    return None


def generate_ccs_project(
    project_dir: Path,
    syscfg_cmd: str = "",
    ccs_path: str = "",
    silent: bool = False,
    timeout: int = 900,
) -> dict:
    """驱动 SysConfig CLI 重生成 C2000 外设产物（+ 可选 CCS 构建）。

    返回 {ok, syscfg, cmd, out_dir, ccs_built?}
    """
    root = project_dir.resolve()
    syscfg = root / "main.syscfg"
    if not syscfg.is_file():
        raise FileNotFoundError(f"{root} 下未找到 main.syscfg")

    exe = _find_sysconfig_cmd(syscfg_cmd)
    if not exe:
        raise FileNotFoundError("找不到 SysConfig CLI — 用 --syscfg-cmd 或设 SYSCFG_CMD")

    out_dir = root / "syscfg"
    cmd = [exe, "-s", str(syscfg), "-o", str(out_dir)]
    if silent:
        cmd.append("--silent")
    LOG.info("SysConfig 命令: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"SysConfig 超时 ({timeout}s)")

    if r.returncode != 0:
        raise RuntimeError(f"SysConfig 退出码 {r.returncode}\n{r.stderr[-400:]}")

    board = out_dir / "board.c"
    if not board.is_file():
        raise RuntimeError(f"SysConfig 产物校验失败: 缺 {board}")

    result: dict = {"ok": True, "syscfg": str(syscfg), "cmd": " ".join(cmd),
                    "out_dir": str(out_dir), "ccs_built": None}

    eclipsec = _find_ccs_eclipsec(ccs_path)
    if eclipsec:
        proj_name = root.name
        cc = [eclipsec, "-noSplash",
              "-application", "com.ti.ccstudio.apps.projectBuild",
              "-ccs.workspace", str(root),
              "-ccs.projects", proj_name]
        LOG.info("CCS 构建: %s", " ".join(cc))
        try:
            br = subprocess.run(cc, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
            result["ccs_built"] = br.returncode == 0
            if br.returncode != 0:
                LOG.warning("CCS 构建失败（syscfg 产物已生成）: %s", br.stderr[-300:])
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.warning("CCS 构建未完成: %s", exc)
            result["ccs_built"] = False
    else:
        LOG.info("未找到 CCS (--ccs-path / CCS_PATH) — 跳过 CCS 构建")

    return result


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    import argparse

    ap = argparse.ArgumentParser(prog="yamc_ccs_generate",
                                 description="c2000 工程自动重生成（SysConfig CLI + 可选 CCS 构建）")
    ap.add_argument("-d", "--directory", required=True, help="工程根（含 main.syscfg）")
    ap.add_argument("--syscfg-cmd", default="", help="SysConfig CLI 路径")
    ap.add_argument("--ccs-path", default="", help="CCS 安装根或 eclipsec.exe 路径")
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args(argv)

    try:
        r = generate_ccs_project(Path(args.directory), args.syscfg_cmd, args.ccs_path,
                                 args.silent, args.timeout)
    except (FileNotFoundError, TimeoutError, RuntimeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"[Pass] SysConfig 重生成完成: {r['syscfg']} → {r['out_dir']}")
    print(f"[Info] board.c 校验通过")
    if r.get("ccs_built") is True:
        print("[Pass] CCS 构建通过")
    elif r.get("ccs_built") is False:
        print("[WARN] CCS 构建失败/未完成（syscfg 产物已生成）")
    return 0


if __name__ == "__main__":
    sys.exit(main())