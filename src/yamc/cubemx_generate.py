"""yamc/cubemx_generate — stm32 工程自动重生成（对标 xr_cubemx_generate）。

定义:「已有工程 → 自动重生成代码」——对已有 .ioc 构造 CubeMX 脚本
(load project → project generate) 驱动 STM32CubeMX 重生成代码，
进程超时 + 产物校验 (Core/Inc + Drivers)；可选 --auto-confirm 弹窗自动确认
(中/英按钮标签识别, 仅 Windows GUI 会话 best-effort)。

不做: 从零创建工程（那是 CubeMX GUI 的职责，yamc 坚持「接入已有工程」）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

POSITIVE_LABELS = (
    "i agree", "accept", "yes", "ok", "continue", "download", "install",
    "migrate", "convert", "finish", "close", "agree",
    "同意", "接受", "是", "确定", "继续", "下载", "安装", "迁移", "转换", "完成", "关闭",
)
DIALOG_KEYWORDS = (
    "migrat", "compat", "convert", "license", "agreement", "accept", "download",
    "install", "package", "software", "firmware", "repository",
    "协议", "许可", "同意", "接受", "下载", "安装", "迁移", "兼容", "转换",
)
STARTUP_KEYWORDS = (
    "user preferences", "project manager settings", "load project",
    "software packs loading failed",
)

DEFAULT_EXPECT_PATHS = ("Core/Inc", "Drivers")


def build_cubemx_script(ioc: Path, generate_code_dir: Optional[str] = None) -> str:
    """构造 CubeMX -q -s 脚本文本。

    标准: load project <ioc> → project generate <dir|默认工程目录>。
    """
    ioc_posix = ioc.resolve().as_posix()
    lines = [f"load project {ioc_posix}"]
    if generate_code_dir:
        lines.append(f"project generate {generate_code_dir}")
    else:
        lines.append("project generate")
        # 兼容: CubeMX 某些版本用 io generate
        lines.append("io generate")
    return "\n".join(lines)


def build_cubemx_command(
    script_path: Path,
    cubemx_cmd: str = "",
    launch_mode: str = "auto",
    java_cmd: str = "",
    silent: bool = False,
) -> list[str]:
    """构造 CubeMX 进程命令（-q -s <script>，可选 -s silent）。"""
    if launch_mode == "java" or (launch_mode == "auto" and (cubemx_cmd or "").lower().endswith(".jar")):
        java = java_cmd or shutil.which("java") or "java"
        jar = cubemx_cmd or _find_cubemx_jar()
        if not jar or not Path(jar).is_file():
            raise FileNotFoundError(f"CubeMX jar 不存在: {jar} — 用 --cubemx-cmd 指定")
        cmd = [java, "-jar", jar, "-q", "-s", str(script_path)]
    else:
        exe = cubemx_cmd or _find_cubemx_exe()
        if not exe:
            raise FileNotFoundError("找不到 STM32CubeMX 可执行文件 — 用 --cubemx-cmd 指定")
        cmd = [exe, "-q", "-s", str(script_path)]
    if silent:
        cmd.append("-s")
    return cmd


def _find_cubemx_exe() -> Optional[str]:
    """探测常见 CubeMX 安装位置（best-effort）。"""
    found = shutil.which("STM32CubeMX")
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        import glob as _g
        for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            cands = [
                f"{drive}:\\ST\\STM32CubeMX\\STM32CubeMX.exe",
                f"{drive}:\\Program Files\\STMicroelectronics\\STM32Cube\\STM32CubeMX\\STM32CubeMX.exe",
            ]
            for c in cands:
                candidates.extend(_g.glob(c))
    else:
        candidates = ["/opt/STM32CubeMX/STM32CubeMX", "/usr/local/STM32CubeMX/STM32CubeMX"]
    for c in candidates:
        if Path(c).is_file():
            return str(c)
    return None


def _find_cubemx_jar() -> Optional[str]:
    if sys.platform == "win32":
        import glob as _g
        for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            matches = _g.glob(f"{drive}:\\ST\\STM32CubeMX\\**\\STM32CubeMX*.jar", recursive=True)
            if matches:
                return matches[0]
    return None


def _confirm_dialog_loop(proc, timeout: float) -> None:
    """best-effort 弹窗自动确认（仅 Windows GUI 会话）：

    周期性枚举顶层窗口, 标题含对话框关键词（license/download/migrate/协议/下载…）
    时向其发送回车（激活默认按钮）。非 Windows 或无桌面会话 → WARN 降级。
    """
    if os.name != "nt":
        LOG.warning("--auto-confirm 仅 Windows 桌面会话可用，降级为普通等待")
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsWindowVisible = user32.IsWindowVisible
        keybd_event = user32.keybd_event
        SetForegroundWindow = user32.SetForegroundWindow
        GetForegroundWindow = user32.GetForegroundWindow

        confirmed = 0
        dead = time.time() + timeout

        def _sweep() -> None:
            nonlocal confirmed
            titles: list[str] = []

            def _cb(hwnd, _lparam):
                if IsWindowVisible(hwnd) and GetWindowTextLengthW(hwnd) > 0:
                    buf = ctypes.create_unicode_buffer(256)
                    GetWindowTextW(hwnd, buf, 256)
                    titles.append(buf.value)
                return True

            user32.EnumWindows(EnumWindowsProc(_cb), 0)
            for title in titles:
                low = title.lower()
                if any(k in low for k in DIALOG_KEYWORDS) and any(k in low for k in STARTUP_KEYWORDS):
                    pass  # 启动类弹窗 - 也尝试确认
                if not any(k in low for k in DIALOG_KEYWORDS):
                    continue
                if any(p in low for p in POSITIVE_LABELS):
                    continue  # 标题本身就是确认按钮文本, 不适用
                win = GetForegroundWindow()
                if win:
                    SetForegroundWindow(win)
                    keybd_event(0x0D, 0, 0, 0)       # VK_RETURN down
                    keybd_event(0x0D, 0, 2, 0)       # VK_RETURN up
                    confirmed += 1
                time.sleep(0.15)
                if confirmed >= 8:
                    return

        while proc.poll() is None and time.time() < dead:
            _sweep()
            time.sleep(0.5)
        if confirmed:
            LOG.info("弹窗自动确认: 发送回车 %d 次", confirmed)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("弹窗自动确认不可用: %s", exc)


def generate_cubemx_project(
    project_dir: Path,
    ioc_file: str = "",
    cubemx_cmd: str = "",
    launch_mode: str = "auto",
    java_cmd: str = "",
    generate_code_dir: str = "",
    expect_paths: Optional[list[str]] = None,
    silent: bool = False,
    auto_confirm: bool = False,
    timeout: int = 1200,
    keep_script: bool = False,
    log_dir: str = "",
) -> dict:
    """驱动 CubeMX 重生成工程代码。

    返回 {ok, ioc, script, cmd, missing}
    """
    root = project_dir.resolve()
    ioc = Path(ioc_file) if ioc_file else next((p for p in sorted(root.rglob("*.ioc")) if "build" not in p.parts), None)
    if ioc is None:
        raise FileNotFoundError(f"{root} 下未找到 .ioc")
    ioc = ioc.resolve()

    script = root / ".yamc_cubemx_script.iocscript"
    if not keep_script:
        script = Path(str(script) + ".tmp")
    script.write_text(build_cubemx_script(ioc, generate_code_dir or ""), encoding="utf-8", newline="\n")

    cmd = build_cubemx_command(script, cubemx_cmd, launch_mode, java_cmd, silent)
    LOG.info("CubeMX 命令: %s", " ".join(cmd))

    log_paths: dict[str, Path] = {}
    if log_dir:
        logdir = Path(log_dir).resolve()
        logdir.mkdir(parents=True, exist_ok=True)
        log_paths = {
            "stdout": logdir / "cubemx.out.log",
            "stderr": logdir / "cubemx.err.log",
        }

    proc = subprocess.Popen(
        cmd,
        stdout=log_paths["stdout"].open("w", encoding="utf-8") if log_paths else subprocess.PIPE,
        stderr=log_paths["stderr"].open("w", encoding="utf-8") if log_paths else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    confirm_thread = None
    if auto_confirm:
        import threading
        confirm_thread = threading.Thread(target=_confirm_dialog_loop, args=(proc, float(timeout)), daemon=True)
        confirm_thread.start()

    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TimeoutError(f"CubeMX 超时 ({timeout}s) — 工程可能未完成生成")

    if proc.returncode != 0:
        tail = (err or "")[-400:]
        raise RuntimeError(f"CubeMX 退出码 {proc.returncode}\n{tail}")

    expect = list(expect_paths or DEFAULT_EXPECT_PATHS)
    missing = [p for p in expect if not (root / p).is_dir()]
    if missing and not generate_code_dir:
        # generate code 到显式目录时校验放宽
        raise RuntimeError(f"生成产物校验失败, 缺: {missing}")

    if not keep_script:
        try:
            script.unlink()
        except OSError:
            pass

    return {"ok": True, "ioc": str(ioc), "script": str(script) if keep_script else None,
            "cmd": cmd, "missing": missing}


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    import argparse

    ap = argparse.ArgumentParser(prog="yamc_cubemx_generate",
                                 description="stm32 工程自动重生成（对标 xr_cubemx_generate）")
    ap.add_argument("-d", "--directory", required=True, help="工程根（含 .ioc）")
    ap.add_argument("--ioc", default="", help="显式 .ioc 路径")
    ap.add_argument("--cubemx-cmd", default="", help="CubeMX 可执行/jar 路径")
    ap.add_argument("--launch-mode", choices=("auto", "direct", "java"), default="auto")
    ap.add_argument("--java-cmd", default="", help="java 可执行路径（-jar 模式）")
    ap.add_argument("--generate-code-dir", default="", help="'generate code' 输出目录")
    ap.add_argument("--expect-path", action="append", default=None, help="生成后必须存在的路径")
    ap.add_argument("--log-dir", default="", help="命令日志目录")
    ap.add_argument("--keep-script", action="store_true", help="保留生成的 .iocscript")
    ap.add_argument("--silent", action="store_true", help="传 -s 给 CubeMX")
    ap.add_argument("--auto-confirm", action="store_true", help="自动确认迁移/许可/下载弹窗（Windows best-effort）")
    ap.add_argument("--timeout", type=int, default=1200, help="进程超时秒数")
    args = ap.parse_args(argv)

    try:
        r = generate_cubemx_project(
            Path(args.directory), args.ioc, args.cubemx_cmd, args.launch_mode,
            args.java_cmd, args.generate_code_dir, args.expect_path,
            args.silent, args.auto_confirm, args.timeout, args.keep_script, args.log_dir)
    except (FileNotFoundError, TimeoutError, RuntimeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"[Pass] CubeMX 重生成完成: {r['ioc']}")
    if r.get("script"):
        print(f"[Info] 脚本保留: {r['script']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())