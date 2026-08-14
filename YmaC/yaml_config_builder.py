#!/usr/bin/env python3
"""
YAML Config Builder — 通用 YAML → C 代码配置注入工具
======================================================
自动发现项目结构，将选中的 YAML 配置渲染为 C designated initializer 并注入目标文件。

双布局支持：
- legacy：conf/*.yaml 配置变体 → User/app/ 或 User/Application/app_main.c
- C-OOP：Config/params/*.yaml → build/gen/<name>/app_main.c（物化副本）

三 Tab GUI：
- Tab1 参数注入：扫描配置变体 → 预览 → 注入 → 编译
- Tab2 拓扑选择：Config/topologies/*.yaml → 生成工程骨架 → 参数表 → 写入/注入/编译
- Tab3 运行时调参：串口 0xFB 帧下发（pyserial 可选）

跨平台：Windows / Debian Linux
原生暗黑模式：跟随系统主题，Windows 暗色标题栏

依赖：pip install PyQt6 pyyaml darkdetect

用法：
    python YmaC/yaml_config_builder.py          # 启动 GUI
    python YmaC/yaml_config_builder.py --cli    # 命令行模式（列出配置）
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

# ── Constants ──────────────────────────────────────────────────
BEGIN_MARKER = "/* CONFIG BEGIN */"
END_MARKER = "/* CONFIG END */"
CONFIG_ID_PATTERN = re.compile(r"/\*\s*config:\s*(.+?)\s*\*/")
C_IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
# A valid C identifier/macro: starts with uppercase, only alphanum + underscore
# e.g. BSP_ADC_VA, BSP_ADC_Ialpha, DT, CAP_MAX_VOLTAGE


# ═══════════════════════════════════════════════════════════════
#  Project Discovery
# ═══════════════════════════════════════════════════════════════

# 工具支持的 legacy 目标子目录（按优先级排列）
_APP_SUBDIRS = [
    ("User", "app"),
    ("User", "Application"),
]
# C-OOP 布局特征目录：Config/ + App/（拓扑/工程/参数 三层 YAML）
_COOP_DIRS = ("Config", "App")


def _first_app_dir(project_root: Path) -> Optional[Path]:
    """返回第一个存在的 User/app/ 或 User/Application/ 目录。"""
    for seg1, seg2 in _APP_SUBDIRS:
        d = project_root / seg1 / seg2
        if d.is_dir():
            return d
    return None


def _is_coop_root(root: Path) -> bool:
    """判断是否为 C-OOP 仓库根（Config/ + App/ 同时存在）。"""
    return all((root / name).is_dir() for name in _COOP_DIRS)


def _find_materialized_target(project_root: Path) -> Optional[Path]:
    """在 build/gen/<name>/ 下查找最近物化的 app_main.c（Tab1 注入目标优先）。"""
    gen_dir = project_root / "build" / "gen"
    if not gen_dir.is_dir():
        return None
    candidates = [p for p in gen_dir.glob("*/app_main.c") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """从 start 向上搜索项目根。

    双模式识别：
    - legacy：含 conf/ 和 User/app/ 或 User/Application/
    - C-OOP：含 Config/ 和 App/
    """
    here = Path(start or os.getcwd()).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "conf").is_dir() and _first_app_dir(parent) is not None:
            return parent
        if _is_coop_root(parent):
            return parent
    return None


def find_target_file(project_root: Path) -> Optional[Path]:
    """查找注入目标文件。

    C-OOP 优先返回 build/gen/<name>/app_main.c（物化副本）；
    legacy 保持原有 User/app/ 或 User/Application/ 扫描逻辑。
    """
    if _is_coop_root(project_root):
        materialized = _find_materialized_target(project_root)
        if materialized is not None:
            return materialized

    for seg1, seg2 in _APP_SUBDIRS:
        app_dir = project_root / seg1 / seg2
        if not app_dir.is_dir():
            continue

        # 优先 app_main.c
        default = app_dir / "app_main.c"
        if default.is_file():
            return default

        # 扫描所有 .c 文件
        for c_file in sorted(app_dir.glob("*.c")):
            try:
                content = c_file.read_text(encoding="utf-8")
                if BEGIN_MARKER in content and END_MARKER in content:
                    return c_file
            except Exception:
                continue

    return None


# ═══════════════════════════════════════════════════════════════
#  YAML → C 渲染引擎
# ═══════════════════════════════════════════════════════════════

def _is_c_identifier(val: str) -> bool:
    """全大写 + 下划线开头 → 视为 C 枚举 / 宏标识符。"""
    return bool(C_IDENTIFIER_PATTERN.match(val))


def _format_float(val: float) -> str:
    """float → C 字面量，负数加括号。"""
    text = f"{val:.10f}f"
    if val < 0:
        return f"({text})"
    return text


def render_value(value: Any, indent_level: int, indent_step: int = 4) -> str:
    """递归渲染单个 YAML 值为 C 字面量字符串。

    参数
    ----
    value : 任意 YAML 节点
    indent_level : 当前缩进级别（0 = 顶层字段值）
    indent_step  : 每级缩进空格数（默认 4）
    """
    prefix = " " * (indent_level * indent_step)

    if isinstance(value, dict):
        items = list(value.items())
        if not items:
            return "{}"
        lines = ["{"]
        for key, val in items:
            rendered = render_value(val, indent_level + 1, indent_step)
            if isinstance(val, dict):
                lines.append(f"{prefix}{' ' * indent_step}.{key} = {rendered},")
            else:
                lines.append(f"{prefix}{' ' * indent_step}.{key} = {rendered},")
        lines.append(f"{prefix}}}")
        return "\n".join(lines)

    if isinstance(value, list):
        items = [render_value(v, indent_level, indent_step) for v in value]
        inner = ", ".join(items)
        return f"{{ {inner} }}"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return _format_float(value)

    if isinstance(value, str):
        if _is_c_identifier(value):
            return value
        return f'"{value}"'

    return str(value)


def render_config_block(config: dict, indent_step: int = 4) -> str:
    """将 YAML `config:` 树渲染为 C designated initializer 块。

    顶层 key 展开为独立条目，用于替换标记之间的内容。
    """
    items = list(config.items())
    if not items:
        return ""

    lines: list[str] = []
    for i, (key, val) in enumerate(items):
        rendered = render_value(val, indent_level=1, indent_step=indent_step)
        comma = "," if i < len(items) - 1 else ""
        if isinstance(val, dict):
            lines.append(f"    .{key} = {rendered}{comma}")
        else:
            lines.append(f"    .{key} = {rendered}{comma}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  File I/O
# ═══════════════════════════════════════════════════════════════

def scan_configs(conf_dir: Path) -> list[dict]:
    """扫描目录下 *.yaml，返回 [{path, config_id, description, config}, ...]。

    兼容 legacy conf/*.yaml 与 C-OOP Config/params/*.yaml（schema 均为
    config_id/description/config）。排除以 _example 或 _template 结尾的文件。
    """
    result: list[dict] = []
    for p in sorted(conf_dir.glob("*.yaml")):
        if p.stem.endswith("_example") or p.stem.endswith("_template"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue
        if "config" not in data:
            continue

        result.append({
            "path": p,
            "config_id": data.get("config_id", p.stem),
            "description": data.get("description", ""),
            "config": data["config"],
        })
    return result


def detect_current_config(target_file: Path) -> Optional[str]:
    """读取目标文件，从 CONFIG BEGIN 后提取 /* config: xxx */ 标记。"""
    try:
        content = target_file.read_text(encoding="utf-8")
    except Exception:
        return None

    begin_idx = content.find(BEGIN_MARKER)
    if begin_idx == -1:
        return None
    end_idx = content.find(END_MARKER, begin_idx)
    if end_idx == -1:
        return None

    block = content[begin_idx:end_idx]
    m = CONFIG_ID_PATTERN.search(block)
    if m:
        return m.group(1).strip()
    return None


def inject_config(target_file: Path, rendered: str, config_id: str) -> bool:
    """将渲染后的 C 块注入目标文件的 CONFIG BEGIN/END 标记之间。"""
    try:
        original = target_file.read_text(encoding="utf-8")
    except Exception:
        return False

    begin_idx = original.find(BEGIN_MARKER)
    if begin_idx == -1:
        return False

    end_idx = original.find(END_MARKER, begin_idx)
    if end_idx == -1:
        return False

    # 提取 BEGIN 行的缩进用作基准
    line_start = original.rfind("\n", 0, begin_idx) + 1
    indent = original[line_start:begin_idx]

    # 构建新块：BEGIN → config 注释 → 渲染内容 → END
    new_block = (
        f"{indent}{BEGIN_MARKER}\n"
        f"{indent}/* config: {config_id} */\n"
        f"{rendered}\n"
        f"{indent}{END_MARKER}"
    )

    updated = original[:line_start] + new_block + original[end_idx + len(END_MARKER):]
    try:
        target_file.write_text(updated, encoding="utf-8")
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  Build System
# ═══════════════════════════════════════════════════════════════

def detect_build(project_root: Path) -> Optional[dict]:
    """自动检测构建系统。支持 CMake（build/ 子目录）。"""
    for build_dir_name in ("build", "build/Debug", "build/Release"):
        bd = project_root / build_dir_name
        if (bd / "CMakeCache.txt").is_file():
            return {
                "type": "cmake",
                "dir": str(bd),
                "label": build_dir_name,
            }
    build_root = project_root / "build"
    if build_root.is_dir():
        for child in sorted(build_root.iterdir()):
            if child.is_dir() and (child / "CMakeCache.txt").is_file():
                return {
                    "type": "cmake",
                    "dir": str(child),
                    "label": f"build/{child.name}",
                }
    return None


def find_cmake() -> Optional[str]:
    """查找 cmake 可执行文件路径。

    先尝试 PATH，再搜索常见安装位置（含 ARM 工具链）。
    """
    import shutil

    cmake = shutil.which("cmake")
    if cmake:
        return cmake

    # 同时尝试搜索所有盘符下的常见工具链目录
    candidates: list[str] = []

    if sys.platform == "win32":
        # 扫描所有盘符下的常见目录
        import glob as _glob
        drive_patterns = [
            r"{drive}:\Program Files\CMake\bin\cmake.exe",
            r"{drive}:\Program Files (x86)\CMake\bin\cmake.exe",
            r"{drive}:\GNU_C_Compiler\bin\cmake.exe",
            r"{drive}:\GNU Arm Embedded Toolchain\bin\cmake.exe",
            r"{drive}:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*\tools\bin\cmake.exe",
        ]
        # 扫描 A-Z 盘符
        import string as _string
        for letter in _string.ascii_uppercase:
            for pattern in drive_patterns:
                p = pattern.format(drive=letter)
                # 用 glob 匹配通配符路径
                if "*" in p:
                    for matched in _glob.glob(p):
                        candidates.append(matched)
                else:
                    candidates.append(p)

        # VS 2022 自带 CMake
        vs_cmake = (
            r"{drive}:\Program Files\Microsoft Visual Studio\2022"
            r"\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        )
        for letter in _string.ascii_uppercase:
            p = vs_cmake.format(drive=letter)
            candidates.append(p)

        # cmake 可能在任何 CMake* 目录下
        for letter in _string.ascii_uppercase:
            for base in [f"{letter}:\\Program Files", f"{letter}:\\Program Files (x86)"]:
                for matched in _glob.glob(f"{base}\\CMake*\\bin\\cmake.exe"):
                    candidates.append(matched)

    # Linux
    for c in ["/usr/bin/cmake", "/usr/local/bin/cmake", "/snap/bin/cmake"]:
        candidates.append(c)

    for c in candidates:
        if Path(c).is_file():
            return c

    return None


def build_command(build_info: dict) -> list[str]:
    """根据构建信息生成 cmake --build 命令。优先使用 find_cmake()。"""
    cmake_path = find_cmake() or "cmake"
    return [cmake_path, "--build", build_info["dir"]]


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def run_cli() -> int:
    """命令行模式：列出配置、应用配置。支持 legacy 与 C-OOP 双布局。"""
    root = find_project_root()
    if root is None:
        print("错误：未找到项目根目录（legacy: **conf/** + **User/app/ 或 User/Application/**；"
              "C-OOP: **Config/** + **App/**）")
        return 1

    target = find_target_file(root)
    if target is None:
        print("错误：未找到含 CONFIG 标记的 .c 文件"
              "（C-OOP 需先在 GUI 生成工程并物化 build/gen/<name>/app_main.c）")
        return 1

    legacy = (root / "conf").is_dir()
    cfg_dir = root / "conf" if legacy else root / "Config" / "params"

    print(f"项目根目录: {root}")
    print(f"目标文件:   {target.relative_to(root)}")
    cfgs = scan_configs(cfg_dir)
    if not cfgs:
        print(f"{cfg_dir} 中未找到有效的 YAML 配置文件")
        return 1

    print(f"\n可用配置变体 ({len(cfgs)} 个):")
    for i, cfg in enumerate(cfgs):
        marker = " *" if detect_current_config(target) == cfg["config_id"] else "  "
        print(f"  {marker} [{i}] {cfg['config_id']:20s} {cfg['description']}")

    if len(sys.argv) > 2:
        # 尝试按 config_id 或索引应用
        sel = sys.argv[2]
        target_cfg = None
        for i, cfg in enumerate(cfgs):
            if sel == cfg["config_id"] or sel == str(i):
                target_cfg = cfg
                break

        if target_cfg is None:
            print(f"未找到配置: {sel}")
            return 1

        rendered = render_config_block(target_cfg["config"])
        if inject_config(target, rendered, target_cfg["config_id"]):
            print(f"✓ 配置 [{target_cfg['config_id']}] 已注入 {target}")
        else:
            print("✗ 注入失败")
            return 1

    return 0


# ═══════════════════════════════════════════════════════════════
#  GUI (lazy import: 仅 main() 调用时加载 PyQt6)
# ═══════════════════════════════════════════════════════════════

def _build_gui() -> int:
    """构建并运行 GUI。PyQt6 在此函数内按需导入。"""
    # ── 延迟导入 PyQt6 ──────────────────────────────────────
    try:
        from PyQt6.QtCore import QProcess, Qt, QThread, QTimer, pyqtSignal
        from PyQt6.QtGui import QColor, QFont, QPalette
        from PyQt6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QDoubleSpinBox,
            QFileDialog,
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QScrollArea,
            QSplitter,
            QTabWidget,
            QTableWidget,
            QTableWidgetItem,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        print(f"PyQt6 未安装。请执行: pip install PyQt6 darkdetect\n详情: {exc}")
        return 1

    # ── 运行时调参（pyserial 可选，未安装时 Tab3 降级为提示）──
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports
        _HAS_SERIAL = True
    except ImportError:
        serial = None
        _HAS_SERIAL = False

    # 确保 YmaC 目录在 sys.path 上（便于 import scaffold）
    _ymac_dir = str(Path(__file__).resolve().parent)
    if _ymac_dir not in sys.path:
        sys.path.insert(0, _ymac_dir)

    # 共享接入流水线 (CLI 同源) + 外部工程探测
    from engine import _LEVEL_TAG, run_pipeline
    from project_probe import detect_platform as _probe_platform
    from project_probe import find_project_root as _probe_root

    # ── 暗色主题工具 ────────────────────────────────────────

    def _is_dark() -> bool:
        try:
            import darkdetect
            return darkdetect.isDark()
        except ImportError:
            return True

    def _win_dark_title(hwnd: int) -> None:
        if sys.platform != "win32":
            return
        try:
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(hwnd),
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(ctypes.c_int(1)),
                ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

    def _apply_dark(app: QApplication) -> None:
        if not _is_dark():
            return

        palette = QPalette()
        base = QColor(30, 30, 30)
        alt_base = QColor(37, 37, 37)
        button = QColor(45, 45, 45)
        highlight = QColor(0, 120, 215)
        text = QColor(220, 220, 220)
        bright = QColor(255, 140, 80)

        palette.setColor(QPalette.ColorRole.Window, base)
        palette.setColor(QPalette.ColorRole.WindowText, text)
        palette.setColor(QPalette.ColorRole.Base, alt_base)
        palette.setColor(QPalette.ColorRole.AlternateBase, base)
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(45, 45, 45))
        palette.setColor(QPalette.ColorRole.ToolTipText, text)
        palette.setColor(QPalette.ColorRole.Text, text)
        palette.setColor(QPalette.ColorRole.Button, button)
        palette.setColor(QPalette.ColorRole.ButtonText, text)
        palette.setColor(QPalette.ColorRole.BrightText, bright)
        palette.setColor(QPalette.ColorRole.Link, QColor(100, 149, 237))
        palette.setColor(QPalette.ColorRole.Highlight, highlight)
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))

        app.setPalette(palette)
        app.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QListWidget {
                background-color: #252525; color: #dcdcdc;
                border: 1px solid #3c3c3c; border-radius: 4px;
                font-size: 14px;
            }
            QListWidget::item { padding: 8px 12px; border-bottom: 1px solid #333; }
            QListWidget::item:selected { background-color: #0078d4; color: #fff; }
            QListWidget::item:hover { background-color: #2a2d2e; }
            QTreeWidget {
                background-color: #1e1e1e; color: #dcdcdc;
                border: 1px solid #3c3c3c; border-radius: 4px;
                font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
                font-size: 13px;
            }
            QTreeWidget::item { padding: 2px 0; }
            QPlainTextEdit {
                background-color: #1e1e1e; color: #b8b8b8;
                border: 1px solid #3c3c3c; border-radius: 4px;
                font-family: 'Consolas', 'DejaVu Sans Mono', monospace;
                font-size: 12px;
            }
            QPushButton {
                background-color: #0078d4; color: #fff;
                border: none; padding: 8px 20px; border-radius: 4px;
                font-weight: bold; font-size: 13px;
            }
            QPushButton:hover { background-color: #1e8ae6; }
            QPushButton:pressed { background-color: #005a9e; }
            QPushButton:disabled { background-color: #3c3c3c; color: #808080; }
            QPushButton#btn_secondary {
                background-color: #3c3c3c; color: #dcdcdc;
            }
            QPushButton#btn_secondary:hover { background-color: #4a4a4a; }
            QLabel { color: #dcdcdc; }
            QGroupBox {
                color: #dcdcdc; border: 1px solid #3c3c3c;
                border-radius: 6px; margin-top: 14px; padding-top: 18px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 12px; padding: 0 6px;
            }
            QSplitter::handle { background-color: #3c3c3c; width: 2px; }
        """)

    # ── 主窗口类 ────────────────────────────────────────────

    class _EngineRunThread(QThread):
        """engine.run_pipeline 后台线程: 日志逐行发射 + 完成信号.

        git/submodule/构建等慢操作不阻塞 UI; line 信号接窗口级日志框."""
        line = pyqtSignal(str)
        done = pyqtSignal(bool, str)

        def __init__(self, start: Path, topology: str, params: Optional[dict],
                     opts: dict) -> None:
            super().__init__()
            self._start = start
            self._topology = topology
            self._params = params
            self._opts = opts

        def run(self) -> None:
            res = run_pipeline(
                self._start, self._topology, self._params, self._opts,
                log=lambda level, msg: self.line.emit(f"[{_LEVEL_TAG.get(level, level.upper())}] {msg}"),
            )
            ok = bool(res.get("ok"))
            reason = res.get("reason") or "完成 — app_main.c/h + CMake 接入就绪"
            self.done.emit(ok, reason)

    class _ConfigBuilderWindow(QMainWindow):
        """YAML Config Builder 主窗口。"""

        _project_root: Optional[Path]
        _configs: list[dict]
        _current_id: Optional[str]
        _build_info: Optional[dict]
        _build_process: Optional[QProcess]
        _engine_thread: Optional[QThread]

        def __init__(self) -> None:
            super().__init__()
            self._project_root = None
            self._configs = []
            self._current_id = None
            self._build_info = None
            self._build_process = None
            self._build_active_btn = None
            self._engine_thread = None
            self._topologies = []
            self._current_topo = None
            self._param_spinboxes = {}
            self._runtime_spinboxes = {}
            self._ser = None
            self._ser_poll = None

            self.setWindowTitle("YAML Config Builder")
            self.setMinimumSize(1040, 680)
            self.resize(1180, 760)
            self._init_ui()
            self._discover_project()

        def _init_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)
            root_layout = QVBoxLayout(central)
            root_layout.setContentsMargins(12, 12, 12, 12)
            root_layout.setSpacing(8)

            # 状态栏（窗口级，三个 Tab 共享）
            status_row = QHBoxLayout()
            self._lbl_project = QLabel("项目: (未检测)")
            self._lbl_project.setStyleSheet("font-weight: bold; font-size: 14px;")
            status_row.addWidget(self._lbl_project)
            status_row.addStretch()
            self._lbl_current = QLabel("当前注入: —")
            self._lbl_current.setStyleSheet("color: #4ec9b0; font-size: 13px;")
            status_row.addWidget(self._lbl_current)
            root_layout.addLayout(status_row)

            # 日志（窗口级共享，Tab 构建期间即可写入）
            mono = QFont("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono", 11)
            self._log = QPlainTextEdit()
            self._log.setReadOnly(True)
            self._log.setMaximumBlockCount(2000)
            self._log.setFont(mono)
            self._log.setMinimumHeight(110)

            # 中央 Tab 控件
            self._tabs = QTabWidget()
            self._build_tab_inject()
            self._build_tab_topology()
            self._build_tab_runtime()
            root_layout.addWidget(self._tabs, stretch=1)

            log_group = QGroupBox("日志")
            log_inner = QVBoxLayout(log_group)
            log_inner.addWidget(self._log)
            root_layout.addWidget(log_group)

        def _build_tab_inject(self) -> None:
            """Tab1「参数注入」— 原单窗口全部交互逻辑整体搬入，行为不变。"""
            widget = QWidget()
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(8)

            # 主分割器
            splitter = QSplitter(Qt.Orientation.Horizontal)

            # 左：配置列表
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            lbl_list = QLabel("配置变体 (*.yaml)")
            lbl_list.setStyleSheet("font-weight: bold; font-size: 13px;")
            left_layout.addWidget(lbl_list)
            self._list_widget = QListWidget()
            self._list_widget.currentRowChanged.connect(self._on_config_selected)
            left_layout.addWidget(self._list_widget)
            splitter.addWidget(left_panel)

            # 右：详情 + 预览
            right_panel = QWidget()
            right_layout = QVBoxLayout(right_panel)
            right_layout.setContentsMargins(0, 0, 0, 0)

            desc_group = QGroupBox("配置详情")
            desc_inner = QVBoxLayout(desc_group)
            self._lbl_desc = QLabel("")
            self._lbl_desc.setWordWrap(True)
            self._lbl_desc.setStyleSheet("color: #888; font-size: 12px; padding: 4px 0;")
            desc_inner.addWidget(self._lbl_desc)
            self._tree = QTreeWidget()
            self._tree.setHeaderLabels(["Key", "Value"])
            self._tree.header().setStretchLastSection(True)
            self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            desc_inner.addWidget(self._tree)
            right_layout.addWidget(desc_group)

            preview_group = QGroupBox("C 代码预览")
            preview_inner = QVBoxLayout(preview_group)
            self._preview = QPlainTextEdit()
            self._preview.setReadOnly(True)
            self._preview.setMaximumBlockCount(5000)
            mono = QFont("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono", 11)
            self._preview.setFont(mono)
            preview_inner.addWidget(self._preview)
            right_layout.addWidget(preview_group)

            splitter.addWidget(right_panel)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 3)
            layout.addWidget(splitter, stretch=1)

            # 操作按钮
            btn_row = QHBoxLayout()
            self._btn_apply = QPushButton("▶ 应用选中配置")
            self._btn_apply.clicked.connect(self._on_apply)
            self._btn_apply.setEnabled(False)
            btn_row.addWidget(self._btn_apply)

            self._btn_build = QPushButton("⚙ 编译")
            self._btn_build.setObjectName("btn_secondary")
            self._btn_build.clicked.connect(self._on_build)
            self._btn_build.setEnabled(False)
            btn_row.addWidget(self._btn_build)

            self._btn_refresh = QPushButton("↻ 刷新")
            self._btn_refresh.setObjectName("btn_secondary")
            self._btn_refresh.clicked.connect(self._on_refresh)
            btn_row.addWidget(self._btn_refresh)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            widget.setLayout(layout)
            self._tabs.addTab(widget, "参数注入")

        def _build_tab_topology(self) -> None:
            """Tab2「拓扑选择」— 扫描 Config/topologies/*.yaml 生成工程骨架。"""
            widget = QWidget()
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(8)

            # 顶部：工程名 / MCU / 变体名
            top_row = QHBoxLayout()
            top_row.addWidget(QLabel("工程名:"))
            self._edit_proj_name = QLineEdit()
            self._edit_proj_name.setPlaceholderText("工程名（默认=拓扑名）")
            top_row.addWidget(self._edit_proj_name, stretch=1)

            top_row.addWidget(QLabel("MCU:"))
            self._combo_mcu = QComboBox()
            self._combo_mcu.setEditable(True)
            self._combo_mcu.addItems(["STM32F334R8", "STM32G474", "TMS320F280049C"])
            self._combo_mcu.setCurrentText("STM32F334R8")
            top_row.addWidget(self._combo_mcu)

            top_row.addWidget(QLabel("变体名:"))
            self._edit_variant = QLineEdit("default")
            self._edit_variant.setMaximumWidth(120)
            top_row.addWidget(self._edit_variant)
            layout.addLayout(top_row)

            # 主分割器：左拓扑列表 / 右详情 + 参数表
            splitter = QSplitter(Qt.Orientation.Horizontal)

            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            lbl_list = QLabel("拓扑 (Config/topologies/*.yaml)")
            lbl_list.setStyleSheet("font-weight: bold; font-size: 13px;")
            left_layout.addWidget(lbl_list)
            self._topo_list = QListWidget()
            self._topo_list.currentRowChanged.connect(self._on_topo_selected)
            left_layout.addWidget(self._topo_list)
            splitter.addWidget(left_panel)

            right_panel = QWidget()
            right_layout = QVBoxLayout(right_panel)
            right_layout.setContentsMargins(0, 0, 0, 0)

            desc_group = QGroupBox("拓扑详情")
            desc_inner = QVBoxLayout(desc_group)
            self._lbl_topo_desc = QLabel("")
            self._lbl_topo_desc.setWordWrap(True)
            self._lbl_topo_desc.setStyleSheet("color: #888; font-size: 12px; padding: 2px 0;")
            desc_inner.addWidget(self._lbl_topo_desc)

            meta_row = QHBoxLayout()
            self._lbl_topo_status = QLabel("")
            self._lbl_topo_status.setStyleSheet("font-weight: bold; font-size: 13px;")
            meta_row.addWidget(self._lbl_topo_status)
            self._lbl_topo_control = QLabel("")
            self._lbl_topo_control.setStyleSheet("color: #4ec9b0; font-size: 12px;")
            meta_row.addWidget(self._lbl_topo_control)
            meta_row.addStretch()
            desc_inner.addLayout(meta_row)

            info_row = QHBoxLayout()
            self._lbl_topo_modules = QLabel("")
            self._lbl_topo_params = QLabel("")
            info_row.addWidget(self._lbl_topo_modules)
            info_row.addStretch()
            info_row.addWidget(self._lbl_topo_params)
            desc_inner.addLayout(info_row)
            right_layout.addWidget(desc_group)

            # 参数表（QFormLayout + QDoubleSpinBox）
            param_group = QGroupBox("参数表（按拓扑 params schema）")
            param_inner = QVBoxLayout(param_group)
            self._param_form = QFormLayout()
            self._param_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            form_host = QWidget()
            form_host.setLayout(self._param_form)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMinimumHeight(160)
            scroll.setWidget(form_host)
            param_inner.addWidget(scroll)
            right_layout.addWidget(param_group, stretch=1)

            splitter.addWidget(right_panel)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 2)
            layout.addWidget(splitter, stretch=1)

            # 操作按钮
            btn_row = QHBoxLayout()
            self._btn_gen = QPushButton("⚒ 生成工程")
            self._btn_gen.setToolTip("仅 status=ready 的拓扑可生成工程")
            self._btn_gen.clicked.connect(self._on_topo_generate)
            self._btn_gen.setEnabled(False)
            btn_row.addWidget(self._btn_gen)

            self._btn_write_params = QPushButton("✎ 写入参数")
            self._btn_write_params.setObjectName("btn_secondary")
            self._btn_write_params.clicked.connect(self._on_write_params)
            btn_row.addWidget(self._btn_write_params)

            self._btn_inject_app = QPushButton("⟳ 注入 App")
            self._btn_inject_app.setObjectName("btn_secondary")
            self._btn_inject_app.clicked.connect(self._on_inject_app)
            btn_row.addWidget(self._btn_inject_app)

            self._btn_topo_build = QPushButton("⚙ 编译")
            self._btn_topo_build.setObjectName("btn_secondary")
            self._btn_topo_build.clicked.connect(self._on_topo_build)
            btn_row.addWidget(self._btn_topo_build)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            # ── 外部工程接入 (ymac_cfg 完整流水线, 与 CLI 同源 engine) ──
            ext_group = QGroupBox("外部工程接入（ymac_cfg 完整流水线 → 真实 CubeMX 工程）")
            ext_inner = QVBoxLayout(ext_group)
            ext_inner.setContentsMargins(8, 12, 8, 8)
            ext_inner.setSpacing(6)

            ext_row = QHBoxLayout()
            ext_row.addWidget(QLabel("工程根:"))
            self._edit_ext_root = QLineEdit()
            self._edit_ext_root.setPlaceholderText("外部工程根（含 .ioc），如 D:/proj/my_psu")
            ext_row.addWidget(self._edit_ext_root, stretch=1)
            self._btn_ext_browse = QPushButton("浏览…")
            self._btn_ext_browse.setObjectName("btn_secondary")
            self._btn_ext_browse.clicked.connect(self._on_ext_browse)
            ext_row.addWidget(self._btn_ext_browse)
            self._btn_ext_probe = QPushButton("探测")
            self._btn_ext_probe.setObjectName("btn_secondary")
            self._btn_ext_probe.clicked.connect(self._on_ext_probe)
            ext_row.addWidget(self._btn_ext_probe)
            ext_inner.addLayout(ext_row)

            self._lbl_ext_probe = QLabel("未探测")
            self._lbl_ext_probe.setStyleSheet("color: #888; font-size: 12px;")
            ext_inner.addWidget(self._lbl_ext_probe)

            opt_row = QHBoxLayout()
            self._chk_ext_no_build = QCheckBox("跳过编译")
            self._chk_ext_no_sub = QCheckBox("跳过 submodule (adopt 已有目录)")
            self._chk_ext_no_sub.setChecked(True)
            opt_row.addWidget(self._chk_ext_no_build)
            opt_row.addWidget(self._chk_ext_no_sub)
            opt_row.addStretch()
            ext_inner.addLayout(opt_row)

            self._btn_ext_run = QPushButton("▶ 运行完整接入")
            self._btn_ext_run.clicked.connect(self._on_ext_run)
            ext_inner.addWidget(self._btn_ext_run)

            layout.addWidget(ext_group)

            widget.setLayout(layout)
            self._tabs.addTab(widget, "拓扑选择")

        def _build_tab_runtime(self) -> None:
            """Tab3「运行时调参」— 串口 0xFB 帧下发（pyserial 可选）。"""
            widget = QWidget()
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(8)

            if not _HAS_SERIAL:
                self._runtime_available = False
                tip = QLabel(
                    "pyserial 未安装，运行时调参不可用。\n请执行: pip install pyserial"
                )
                tip.setWordWrap(True)
                tip.setStyleSheet("color: #e5b567; font-size: 14px; padding: 12px;")
                layout.addWidget(tip)
                layout.addStretch()
                widget.setLayout(layout)
                self._tabs.addTab(widget, "运行时调参")
                return
            self._runtime_available = True

            # 串口行
            port_row = QHBoxLayout()
            port_row.addWidget(QLabel("串口:"))
            self._combo_port = QComboBox()
            self._combo_port.setMinimumWidth(240)
            port_row.addWidget(self._combo_port)
            btn_ports = QPushButton("↻ 刷新")
            btn_ports.setObjectName("btn_secondary")
            btn_ports.clicked.connect(self._on_refresh_ports)
            port_row.addWidget(btn_ports)

            port_row.addWidget(QLabel("波特率:"))
            self._combo_baud = QComboBox()
            self._combo_baud.setEditable(True)
            self._combo_baud.addItems(["9600", "19200", "38400", "57600", "115200", "230400"])
            self._combo_baud.setCurrentText("115200")
            port_row.addWidget(self._combo_baud)

            self._btn_serial = QPushButton("连接")
            self._btn_serial.clicked.connect(self._on_toggle_serial)
            port_row.addWidget(self._btn_serial)
            port_row.addStretch()
            layout.addLayout(port_row)

            # 参数表
            table_group = QGroupBox("参数表（复用当前拓扑 params）")
            table_inner = QVBoxLayout(table_group)
            self._runtime_table = QTableWidget(0, 3)
            self._runtime_table.setHorizontalHeaderLabels(["Key", "参数", "值"])
            self._runtime_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents)
            self._runtime_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch)
            self._runtime_table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.ResizeToContents)
            self._runtime_table.verticalHeader().setVisible(False)
            table_inner.addWidget(self._runtime_table)
            layout.addWidget(table_group, stretch=1)

            # 下发按钮
            btn_row = QHBoxLayout()
            self._btn_send = QPushButton("▶ 下发参数 (0xFB)")
            self._btn_send.clicked.connect(self._on_send_params)
            self._btn_send.setEnabled(False)
            btn_row.addWidget(self._btn_send)
            btn_row.addStretch()
            layout.addLayout(btn_row)

            widget.setLayout(layout)
            self._tabs.addTab(widget, "运行时调参")

            # 应答轮询定时器
            self._ser_poll = QTimer(self)
            self._ser_poll.setInterval(200)
            self._ser_poll.timeout.connect(self._poll_serial)

            self._on_refresh_ports()

        # ── 项目发现 ──────────────────────────────────────

        def _discover_project(self) -> None:
            root = find_project_root()
            if root is None:
                self._log_msg("⚠ 未找到项目根目录（legacy: conf/ + User/app/；C-OOP: Config/ + App/）")
                self._lbl_project.setText("项目: 未找到")
                return

            self._project_root = root
            self._is_coop = _is_coop_root(root)
            self._lbl_project.setText(f"项目: {root.name}  ({root})")

            if self._is_coop:
                params_dir = root / "Config" / "params"
                self._configs = scan_configs(params_dir)
                self._log_msg(f"扫描 Config/params/ → {len(self._configs)} 个参数变体")
                target = _find_materialized_target(root)
                if target is None:
                    self._log_msg("⚠ 未找到物化的 app_main.c（Tab2 生成工程后创建 build/gen/<name>/app_main.c）")
                    self._target_file = None
                else:
                    self._target_file = target
                    self._log_msg(f"注入目标: {target.relative_to(root)}")
            else:
                conf_dir = root / "conf"
                self._configs = scan_configs(conf_dir)
                self._log_msg(f"扫描 conf/ → {len(self._configs)} 个配置变体")
                self._target_file = find_target_file(root)
                if self._target_file is None:
                    self._log_msg("⚠ User/app/ 或 User/Application/ 中未找到含 CONFIG 标记的 .c 文件")

            self._current_id = None
            if self._target_file is not None:
                self._current_id = detect_current_config(self._target_file)
            if self._current_id:
                self._lbl_current.setText(f"当前注入: {self._current_id}")
            else:
                self._lbl_current.setText("当前注入: 未知")

            self._build_info = detect_build(root)
            if self._build_info:
                self._log_msg(f"检测到构建系统: {self._build_info['type']} ({self._build_info['label']})")
                self._btn_build.setEnabled(True)
            else:
                self._log_msg("⚠ 未检测到构建系统（build/CMakeCache.txt）")
                self._btn_build.setEnabled(False)

            self._refresh_config_list()
            self._load_topologies()

        def _refresh_config_list(self) -> None:
            """重建 Tab1 配置变体列表（写入参数后刷新用）。"""
            self._list_widget.clear()
            for cfg in self._configs:
                item = QListWidgetItem(cfg["config_id"])
                item.setToolTip(cfg["description"] or cfg["config_id"])
                if cfg["config_id"] == self._current_id:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self._list_widget.addItem(item)

            if self._current_id:
                for i, cfg in enumerate(self._configs):
                    if cfg["config_id"] == self._current_id:
                        self._list_widget.setCurrentRow(i)
                        break
            elif self._configs:
                self._list_widget.setCurrentRow(0)

            self._btn_apply.setEnabled(len(self._configs) > 0 and self._target_file is not None)

        def _load_topologies(self) -> None:
            """扫描 Config/topologies/*.yaml，填充 Tab2 拓扑列表。"""
            self._topologies = []
            self._topo_list.clear()
            if self._project_root is None:
                return
            topo_dir = self._project_root / "Config" / "topologies"
            if not topo_dir.is_dir():
                self._log_msg("⚠ 未找到 Config/topologies/ 目录")
                return
            for p in sorted(topo_dir.glob("*.yaml")):
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        data = yaml.safe_load(fh)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                self._topologies.append(data)
                name = str(data.get("name") or p.stem)
                display = str(data.get("display") or name)
                if str(data.get("status") or "planned") != "ready":
                    display += "  (待实现)"
                item = QListWidgetItem(display)
                item.setToolTip(str(data.get("description") or display))
                self._topo_list.addItem(item)
            self._log_msg(f"扫描 Config/topologies/ → {len(self._topologies)} 个拓扑")
            if self._topologies:
                # 默认选中第一个 ready 拓扑，否则选中第一个
                idx = 0
                for i, t in enumerate(self._topologies):
                    if str(t.get("status") or "") == "ready":
                        idx = i
                        break
                self._topo_list.setCurrentRow(idx)

        def _on_topo_selected(self, row: int) -> None:
            if row < 0 or row >= len(self._topologies):
                return
            topo = self._topologies[row]
            self._current_topo = topo
            status = str(topo.get("status") or "planned")
            ready = status == "ready"

            self._lbl_topo_desc.setText(str(topo.get("description") or ""))
            color = "#4ec9b0" if ready else "#e5b567"
            self._lbl_topo_status.setStyleSheet(f"font-weight: bold; font-size: 13px; color: {color};")
            self._lbl_topo_status.setText("状态: ready" if ready else "状态: planned (待实现)")

            cm = topo.get("control_module") or "—"
            self._lbl_topo_control.setText(f"控制模块: {cm}")
            modules = topo.get("modules") or []
            params = topo.get("params") or []
            self._lbl_topo_modules.setText(f"模块数: {len(modules)}")
            self._lbl_topo_params.setText(f"参数数: {len(params)}")

            self._btn_gen.setEnabled(ready)
            self._edit_proj_name.setText(str(topo.get("name") or ""))
            self._populate_param_form(params)
            self._populate_runtime_table(params)

        # ── 事件 ──────────────────────────────────────────

        def _on_config_selected(self, row: int) -> None:
            if row < 0 or row >= len(self._configs):
                return
            cfg = self._configs[row]
            self._lbl_desc.setText(f"[{cfg['config_id']}]  {cfg['description'] or '（无描述）'}")

            self._tree.clear()
            self._populate_tree(self._tree.invisibleRootItem(), cfg["config"])
            self._tree.expandAll()

            try:
                self._preview.setPlainText(render_config_block(cfg["config"]))
            except Exception as exc:
                self._preview.setPlainText(f"/* 渲染错误: {exc} */")

        def _on_apply(self) -> None:
            row = self._list_widget.currentRow()
            if row < 0:
                return
            cfg = self._configs[row]
            cid = cfg["config_id"]

            if self._project_root is None:
                QMessageBox.warning(self, "错误", "未找到项目根目录。")
                return

            target = getattr(self, "_target_file", None)
            if target is None:
                QMessageBox.warning(self, "无注入目标", "未找到注入目标（C-OOP 需先在拓扑 Tab 生成工程并物化 app_main.c）。")
                return
            try:
                rendered = render_config_block(cfg["config"])
            except Exception as exc:
                QMessageBox.critical(self, "渲染错误", str(exc))
                return

            if inject_config(target, rendered, cid):
                self._current_id = cid
                self._lbl_current.setText(f"当前注入: {cid}")
                self._log_msg(f"✓ 配置 [{cid}] 已注入 {target}")
                for i in range(self._list_widget.count()):
                    item = self._list_widget.item(i)
                    if item is None:
                        continue
                    f = item.font()
                    f.setBold(item.text() == cid)
                    item.setFont(f)
            else:
                QMessageBox.critical(
                    self, "注入失败",
                    f"无法写入目标文件。\n请确认 {target} 中存在\n{BEGIN_MARKER} ... {END_MARKER}"
                )

        # ── Tab1 编译 ─────────────────────────────────────

        def _on_build(self) -> None:
            # 如果正在编译中，点击按钮 → 取消编译
            if self._build_process is not None:
                self._kill_build()
                return

            if self._build_info is None:
                QMessageBox.warning(self, "错误", "未检测到构建系统。")
                return

            # 预检 cmake 是否可用
            cmake_path = find_cmake()
            if cmake_path is None:
                QMessageBox.critical(
                    self, "cmake 未找到",
                    "系统中未找到 cmake。\n\n"
                    "请确认 cmake 已安装。\n"
                    "下载: https://cmake.org/download/\n\n"
                    "如果已安装但仍报错，请将 cmake 的 bin 目录\n"
                    "添加到系统 PATH 环境变量后重启本工具。"
                )
                self._log_msg("✗ 编译中止：找不到 cmake")
                return
            self._log_msg(f"cmake: {cmake_path}")

            row = self._list_widget.currentRow()
            if row >= 0:
                cfg = self._configs[row]
                if cfg["config_id"] != self._current_id:
                    reply = QMessageBox.question(
                        self, "配置未应用",
                        f"配置 [{cfg['config_id']}] 尚未应用到目标文件。\n是否先应用再编译？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply == QMessageBox.StandardButton.Yes:
                        self._on_apply()
                        if cfg["config_id"] != self._current_id:
                            return

            cmd = build_command(self._build_info)
            self._launch_build(cmd, str(self._project_root or os.getcwd()), self._btn_build)

        def _launch_build(self, cmd: list[str], workdir: str, btn: QPushButton) -> None:
            """启动 cmake 子进程（Tab1 / Tab2 编译共用）。"""
            self._log_msg(f"$ {' '.join(cmd)}")
            self._build_active_btn = btn
            btn.setText("⏹ 取消编译")
            btn.setStyleSheet(
                "QPushButton { background-color: #c42b1c; color: #fff;"
                "border: none; padding: 8px 20px; border-radius: 4px;"
                "font-weight: bold; font-size: 13px; }"
                "QPushButton:hover { background-color: #e04337; }"
            )

            proc = QProcess(self)
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proc.readyReadStandardOutput.connect(self._on_build_output)
            proc.finished.connect(self._on_build_finished)
            proc.errorOccurred.connect(self._on_build_error)
            proc.setWorkingDirectory(workdir)
            self._build_process = proc
            proc.start(cmd[0], cmd[1:])

        def _kill_build(self) -> None:
            if self._build_process is None:
                return
            self._log_msg("⚠ 用户取消编译")
            self._build_process.kill()
            self._build_process.waitForFinished(3000)
            self._build_process = None
            self._reset_build_button()

        def _reset_build_button(self) -> None:
            btn = self._build_active_btn
            if btn is None:
                return
            btn.setEnabled(True)
            btn.setText("⚙ 编译")
            btn.setStyleSheet("")
            self._build_active_btn = None

        def _on_build_output(self) -> None:
            if self._build_process is None:
                return
            data = self._build_process.readAllStandardOutput()
            text = bytes(data).decode("utf-8", errors="replace")
            if text.strip():
                self._log.insertPlainText(text)
                sb = self._log.verticalScrollBar()
                if sb is not None:
                    sb.setValue(sb.maximum())

        def _on_build_error(self, error: QProcess.ProcessError) -> None:
            error_msgs = {
                QProcess.ProcessError.FailedToStart: "进程无法启动（找不到 cmake 或权限不足）",
                QProcess.ProcessError.Crashed: "编译进程崩溃",
                QProcess.ProcessError.Timedout: "编译超时",
                QProcess.ProcessError.WriteError: "写入错误（无法向进程发送数据）",
                QProcess.ProcessError.ReadError: "读取错误（无法读取进程输出）",
                QProcess.ProcessError.UnknownError: "未知错误",
            }
            msg = error_msgs.get(error, f"错误代码: {error}")
            self._log_msg(f"✗ 编译错误: {msg}")
            self._build_process = None
            self._reset_build_button()

        def _on_build_finished(self, exit_code: int, _exit_status) -> None:
            step = getattr(self, "_topo_build_step", "build")
            if step == "configure":
                # Tab2：cmake 配置完成后自动进入 --build
                self._topo_build_step = None
                self._reset_build_button()
                if exit_code != 0:
                    self._log_msg(f"✗ cmake 配置失败 (exit code {exit_code})")
                    self._build_process = None
                    return
                self._log_msg("✓ cmake 配置成功，继续编译")
                name = self._edit_proj_name.text().strip() or ""
                gen_dir = self._project_root / "build" / "gen" / name
                build_dir = gen_dir / "build"
                cmd = [find_cmake() or "cmake", "--build", str(build_dir)]
                self._launch_build(cmd, str(gen_dir), self._btn_topo_build)
                return
            if exit_code == 0:
                self._log_msg("✓ 编译成功")
            else:
                self._log_msg(f"✗ 编译失败 (exit code {exit_code})")
            self._reset_build_button()
            self._build_process = None

        def _on_refresh(self) -> None:
            self._log_msg("— 刷新 —")
            self._list_widget.clear()
            self._tree.clear()
            self._preview.clear()
            self._discover_project()

        # ── Tab2 拓扑选择 ─────────────────────────────────

        def _on_topo_generate(self) -> None:
            if self._current_topo is None:
                QMessageBox.warning(self, "未选择拓扑", "请先在左侧选择拓扑。")
                return
            topo = self._current_topo
            if str(topo.get("status") or "planned") != "ready":
                QMessageBox.warning(self, "拓扑未就绪", "该拓扑控制模块待实现")
                self._log_msg("✗ 生成中止：拓扑状态为 planned（控制模块待实现）")
                return
            if self._project_root is None:
                QMessageBox.critical(self, "生成失败", "未找到项目根目录。")
                return
            root = self._project_root
            name = self._edit_proj_name.text().strip() or str(topo.get("name") or "project")
            mcu = self._combo_mcu.currentText().strip()
            modules = topo.get("modules") or []
            if not modules:
                QMessageBox.critical(self, "生成失败", "拓扑缺少 modules 列表。")
                return

            # 1. 合成 Config/projects/<name>.yaml
            proj_yaml = {
                "project": name,
                "mcu": mcu,
                "description": topo.get("description") or f"{topo.get('display', name)} 工程",
                "modules": modules,
            }
            proj_path = root / "Config" / "projects" / f"{name}.yaml"
            try:
                proj_path.parent.mkdir(parents=True, exist_ok=True)
                proj_path.write_text(
                    yaml.safe_dump(proj_yaml, allow_unicode=True, sort_keys=False, default_flow_style=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                self._log_msg(f"✗ 写入 {proj_path} 失败: {exc}")
                QMessageBox.critical(self, "写入失败", str(exc))
                return

            # 2. import scaffold → cmd_gen 生成骨架
            try:
                import scaffold
            except ImportError:
                self._log_msg("✗ 无法导入 YmaC/scaffold.py")
                QMessageBox.critical(self, "scaffold 缺失", "YmaC/scaffold.py 导入失败。")
                return
            try:
                ret = scaffold.cmd_gen(root, proj_path.relative_to(root).as_posix(), None)
            except Exception as exc:
                self._log_msg(f"✗ scaffold.cmd_gen 失败: {exc}")
                QMessageBox.critical(self, "骨架生成失败", str(exc))
                return
            if ret != 0:
                self._log_msg("✗ scaffold.cmd_gen 返回非零，骨架未生成")
                return

            # 3. 物化 App 模板 → build/gen/<name>/app_main.c
            gen_dir = root / "build" / "gen" / name
            try:
                gen_dir.mkdir(parents=True, exist_ok=True)
                src_c = root / "App" / "app_main.c.tmpl"
                src_h = root / "App" / "app_main.h.tmpl"
                if src_c.is_file():
                    (gen_dir / "app_main.c").write_bytes(src_c.read_bytes())
                if src_h.is_file():
                    (gen_dir / "app_main.h").write_bytes(src_h.read_bytes())
            except Exception as exc:
                self._log_msg(f"✗ 物化 App 模板失败: {exc}")
                QMessageBox.critical(self, "物化失败", str(exc))
                return

            # 4. 刷新 Tab1 注入目标 → 指向新物化副本
            self._target_file = gen_dir / "app_main.c"
            self._current_id = detect_current_config(self._target_file)
            if self._current_id:
                self._lbl_current.setText(f"当前注入: {self._current_id}")
            else:
                self._lbl_current.setText("当前注入: 未知")
            self._refresh_config_list()

            self._log_msg(f"✓ 工程 [{name}] 生成完成: {gen_dir}")
            self._log_msg(f"✓ Config/projects/{name}.yaml 已写入（MCU={mcu}, {len(modules)} 模块）")
            self._log_msg(f"✓ App 模板已物化为 build/gen/{name}/app_main.c")
            QMessageBox.information(self, "生成完成", f"工程 [{name}] 骨架已生成，可在 Tab1 注入参数。")

        def _on_write_params(self) -> None:
            if self._current_topo is None:
                QMessageBox.warning(self, "未选择拓扑", "请先选择拓扑。")
                return
            if self._project_root is None:
                QMessageBox.critical(self, "写入失败", "未找到项目根目录。")
                return
            name = self._edit_proj_name.text().strip() or str(self._current_topo.get("name") or "project")
            variant = self._edit_variant.text().strip() or "default"
            config = self._build_config_from_form()

            doc = {
                "config_id": f"{name}_{variant}",
                "description": f"{self._current_topo.get('display', name)} 参数（{variant} 变体）",
                "config": config,
            }
            params_dir = self._project_root / "Config" / "params"
            out_path = params_dir / f"{name}_{variant}.yaml"
            try:
                params_dir.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                self._log_msg(f"✗ 写入参数失败: {exc}")
                QMessageBox.critical(self, "写入失败", str(exc))
                return
            self._log_msg(f"✓ 参数已写入 {out_path}")
            # 刷新 Tab1 列表，新变体立即可选
            if self._is_coop:
                self._configs = scan_configs(self._project_root / "Config" / "params")
            self._refresh_config_list()

        def _on_inject_app(self) -> None:
            if self._current_topo is None:
                QMessageBox.warning(self, "未选择拓扑", "请先选择拓扑。")
                return
            if self._project_root is None:
                QMessageBox.critical(self, "注入失败", "未找到项目根目录。")
                return
            name = self._edit_proj_name.text().strip() or str(self._current_topo.get("name") or "project")
            target = self._project_root / "build" / "gen" / name / "app_main.c"
            if not target.is_file():
                QMessageBox.warning(self, "未物化", f"{target} 不存在。\n请先点击「生成工程」物化 App 模板。")
                return
            config = self._build_config_from_form()
            config_id = f"{name}_{self._edit_variant.text().strip() or 'default'}"
            try:
                rendered = render_config_block(config)
            except Exception as exc:
                QMessageBox.critical(self, "渲染错误", str(exc))
                return
            if inject_config(target, rendered, config_id):
                self._current_id = config_id
                self._lbl_current.setText(f"当前注入: {config_id}")
                self._log_msg(f"✓ 配置 [{config_id}] 已注入 {target}")
            else:
                QMessageBox.critical(self, "注入失败", f"无法写入 {target}。")

        def _on_topo_build(self) -> None:
            if self._build_process is not None:
                self._kill_build()
                return
            if self._current_topo is None or self._project_root is None:
                QMessageBox.warning(self, "错误", "未选择拓扑或未找到项目根目录。")
                return
            name = self._edit_proj_name.text().strip() or str(self._current_topo.get("name") or "project")
            gen_dir = self._project_root / "build" / "gen" / name
            if not gen_dir.is_dir():
                QMessageBox.warning(self, "未生成", f"{gen_dir} 不存在，请先「生成工程」。")
                return
            cmake_path = find_cmake()
            if cmake_path is None:
                QMessageBox.critical(self, "cmake 未找到", "系统中未找到 cmake。")
                self._log_msg("✗ 编译中止：找不到 cmake")
                return
            self._log_msg(f"cmake: {cmake_path}")

            build_dir = gen_dir / "build"
            if not (build_dir / "CMakeCache.txt").is_file():
                # 未配置 → 先 cmake -S/-B，完成后自动 --build
                self._topo_build_step = "configure"
                cmd = [cmake_path, "-S", str(gen_dir), "-B", str(build_dir)]
            else:
                self._topo_build_step = "build"
                cmd = [cmake_path, "--build", str(build_dir)]
            self._launch_build(cmd, str(gen_dir), self._btn_topo_build)

        # ── 外部工程接入 (engine 后台线程) ───────────────────

        def _on_ext_browse(self) -> None:
            start = self._edit_ext_root.text().strip() or str(Path.home())
            d = QFileDialog.getExistingDirectory(self, "选择外部工程根", start)
            if d:
                self._edit_ext_root.setText(d)
                self._on_ext_probe()

        def _on_ext_probe(self) -> None:
            text = self._edit_ext_root.text().strip()
            if not text:
                self._lbl_ext_probe.setText("未探测")
                return
            root = _probe_root(Path(text))
            if root is None:
                self._lbl_ext_probe.setText(f"⚠ 未找到工程根（{text} 向上无 .ioc/.syscfg）")
                return
            plat = _probe_platform(root)
            self._lbl_ext_probe.setText(f"工程根: {root}  |  平台: {plat}")

        def _on_ext_run(self) -> None:
            text = self._edit_ext_root.text().strip()
            if not text:
                QMessageBox.warning(self, "未选工程根", "请先填写或浏览外部工程根。")
                return
            if self._current_topo is None:
                QMessageBox.warning(self, "未选择拓扑", "请先在左侧选择拓扑。")
                return
            if self._engine_thread is not None and self._engine_thread.isRunning():
                QMessageBox.information(self, "正在运行", "流水线执行中，请稍候。")
                return
            topo_name = str(self._current_topo.get("name") or "buck")
            params = self._build_config_from_form()  # {"power": {...}} → engine 归一化平铺
            opts = {
                "no_submodule": self._chk_ext_no_sub.isChecked(),
                "no_build": self._chk_ext_no_build.isChecked(),
            }
            self._btn_ext_run.setEnabled(False)
            self._log_msg(f"──────────────── 外部工程接入开始: {topo_name} → {text} ────────────────")
            self._engine_thread = _EngineRunThread(Path(text), topo_name, params, opts)
            self._engine_thread.line.connect(self._log_msg)
            self._engine_thread.done.connect(self._on_ext_done)
            self._engine_thread.start()

        def _on_ext_done(self, ok: bool, reason: str) -> None:
            self._btn_ext_run.setEnabled(True)
            # 流水线已把最终 [Pass]/[FAIL] 行写入日志, 这里只弹结果框避免重复
            if ok:
                QMessageBox.information(self, "接入完成", reason)
            else:
                QMessageBox.critical(self, "接入失败", reason)

        # ── Tab3 运行时调参 ───────────────────────────────

        def _on_refresh_ports(self) -> None:
            try:
                ports = list(serial.tools.list_ports.comports())
            except Exception:
                self._log_msg("✗ 无法枚举串口")
                return
            self._combo_port.clear()
            for p in ports:
                desc = p.description or p.device
                self._combo_port.addItem(f"{p.device} — {desc}", p.device)
            if not ports:
                self._combo_port.addItem("(无串口)")
            self._log_msg(f"枚举到 {len(ports)} 个串口")

        def _on_toggle_serial(self) -> None:
            if self._ser is not None and self._ser.is_open:
                self._close_serial()
                return
            port_dev = self._combo_port.currentData()
            if not port_dev:
                QMessageBox.warning(self, "未选串口", "请选择串口设备。")
                return
            try:
                baud = int(self._combo_baud.currentText())
            except ValueError:
                QMessageBox.warning(self, "波特率错误", "请输入合法的整数波特率。")
                return
            try:
                self._ser = serial.Serial(port_dev, baud, timeout=0.05)
            except Exception as exc:
                self._log_msg(f"✗ 串口打开失败: {exc}")
                QMessageBox.critical(self, "连接失败", str(exc))
                return
            self._log_msg(f"✓ 已连接 {port_dev} @ {baud}")
            self._btn_serial.setText("断开")
            self._btn_send.setEnabled(True)
            if self._ser_poll is not None:
                self._ser_poll.start()

        def _close_serial(self) -> None:
            if self._ser_poll is not None:
                self._ser_poll.stop()
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
            if getattr(self, "_runtime_available", False):
                self._btn_serial.setText("连接")
                self._btn_send.setEnabled(False)
                self._log_msg("✓ 串口已断开")

        def _poll_serial(self) -> None:
            if self._ser is None or not self._ser.is_open:
                return
            try:
                n = self._ser.in_waiting
            except Exception:
                return
            if n <= 0:
                return
            try:
                data = self._ser.read(n)
            except Exception as exc:
                self._log_msg(f"✗ 串口读取失败: {exc}")
                return
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = repr(data)
            if text.strip():
                self._log_msg(f"RX: {text.strip()}")

        def _on_send_params(self) -> None:
            if self._ser is None or not self._ser.is_open:
                QMessageBox.warning(self, "未连接", "请先连接串口。")
                return
            if self._current_topo is None:
                QMessageBox.warning(self, "未选择拓扑", "请先选择拓扑。")
                return
            params = self._current_topo.get("params") or []
            tune = self._current_topo.get("tune") or {}
            frame = bytearray(48)
            frame[0] = 0x00  # HEAD
            frame[1] = 0x14  # CMD
            coef = [0.0] * 10
            for p in params:
                key = str(p.get("key", ""))
                slot = int(p.get("slot", -1))
                box = self._runtime_spinboxes.get(key)
                if 0 <= slot < 10 and box is not None:
                    coef[slot] = box.value()
            for i in range(10):
                struct.pack_into("<f", frame, 2 + i * 4, coef[i])
            check = float(tune.get("check_code", 3.1415927))
            struct.pack_into("<f", frame, 42, check)  # [42-45] 校验码 π
            try:
                self._ser.write(bytes(frame))
            except Exception as exc:
                self._log_msg(f"✗ 下发失败: {exc}")
                QMessageBox.critical(self, "下发失败", str(exc))
                return
            nz = sum(1 for c in coef if c != 0.0)
            self._log_msg(f"✓ 已下发 48 字节 0xFB 帧（{nz} 个非零系数，check=π）")

        # ── 辅助 ──────────────────────────────────────────

        def _populate_tree(self, parent: QTreeWidgetItem, data):
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, dict):
                        node = QTreeWidgetItem(parent, [key, ""])
                        self._populate_tree(node, val)
                    elif isinstance(val, list):
                        node = QTreeWidgetItem(parent, [key, f"[{len(val)} 个元素]"])
                        self._populate_tree(node, val)
                    else:
                        QTreeWidgetItem(parent, [key, self._fmt(val)])
            elif isinstance(data, list):
                for i, val in enumerate(data):
                    if isinstance(val, (dict, list)):
                        node = QTreeWidgetItem(parent, [f"[{i}]", ""])
                        self._populate_tree(node, val)
                    else:
                        QTreeWidgetItem(parent, [f"[{i}]", self._fmt(val)])
            else:
                QTreeWidgetItem(parent, ["", self._fmt(data)])

        @staticmethod
        def _fmt(val) -> str:
            if isinstance(val, float):
                return f"{val:.10f}"
            if isinstance(val, bool):
                return "true" if val else "false"
            return str(val)

        def _log_msg(self, msg: str) -> None:
            self._log.appendPlainText(msg)
            sb = self._log.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())

        @staticmethod
        def _set_dotted(config: dict, dotted_key: str, value: float) -> None:
            """dotted key（如 pid_v.kp）展开为嵌套 dict 并赋值。"""
            parts = dotted_key.split(".")
            node = config
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

        def _build_config_from_form(self) -> dict:
            """由 Tab2 参数表单收集 config 树 → 嵌套到拓扑控制域 (默认 .power)。

            C-OOP 注入目标是 ProjectConfig 根结构体: 拓扑域字段必须挂在 .power 下,
            否则渲染出的 .vref/.pid_v 顶层条目无法编译 (ProjectConfig 没有这些成员)。
            非槽位字段 (PWM 通道/占空比限幅, ADC 通道) 不是可调参数, 但必须一并覆盖,
            否则注入会把模板手写默认值整体替换掉 → duty_max=0 等 → 运行时损坏。
            非槽位值取自拓扑 yaml 的 pwm/adc 段 (Config/topologies/<topo>.yaml)。
            """
            inner: dict = {}
            for key, box in self._param_spinboxes.items():
                self._set_dotted(inner, key, box.value())
            self._merge_non_slot(inner)
            return {"power": inner}

        def _merge_non_slot(self, inner: dict) -> None:
            """把拓扑 pwm/adc 段的非槽位字段并入 inner (不覆盖表单已有值)。

            pwm.ch_drive/duty_min/duty_max → ModBuckCfg 同名字段;
            adc.roles.<vout|iout|vin>.ch → ModBuckCfg.adc_ch_<role>。
            freq_hz/sync_rect/mode/deadtime_ns 不属 ModBuckCfg, 不注入。
            """
            topo = self._current_topo or {}
            pwm = topo.get("pwm") or {}
            if isinstance(pwm, dict):
                for f in ("ch_drive", "duty_min", "duty_max"):
                    if f in pwm:
                        inner.setdefault(f, pwm[f])
            adc = topo.get("adc") or {}
            roles = adc.get("roles") or {}
            ch_map = {"vout": "adc_ch_vout", "iout": "adc_ch_iout", "vin": "adc_ch_vin"}
            for role, field in ch_map.items():
                role_cfg = roles.get(role) or {}
                if "ch" in role_cfg:
                    inner.setdefault(field, role_cfg["ch"])

        def _populate_param_form(self, params: list[dict]) -> None:
            """按 params schema 用 QFormLayout + QDoubleSpinBox 渲染 Tab2 参数表。"""
            while self._param_form.rowCount() > 0:
                self._param_form.removeRow(0)
            self._param_spinboxes.clear()
            for p in params:
                key = str(p.get("key", ""))
                if not key:
                    continue
                label = str(p.get("label") or key)
                unit = str(p.get("unit") or "")
                lo = float(p.get("min", 0.0))
                hi = float(p.get("max", 1000.0))
                default = float(p.get("default", lo))
                box = QDoubleSpinBox()
                box.setRange(min(lo, hi), max(lo, hi))
                box.setDecimals(6)
                box.setValue(default)
                if unit:
                    box.setSuffix(f" {unit}")
                tip = label
                if p.get("slot") is not None:
                    tip += f"  (slot {p['slot']})"
                box.setToolTip(tip)
                self._param_form.addRow(QLabel(label), box)
                self._param_spinboxes[key] = box

        def _populate_runtime_table(self, params: list[dict]) -> None:
            """复用当前拓扑 params 渲染 Tab3 参数表（值可编辑，用于 0xFB 下发）。"""
            if not getattr(self, "_runtime_available", False):
                return
            self._runtime_table.setRowCount(0)
            self._runtime_spinboxes.clear()
            for p in params:
                key = str(p.get("key", ""))
                if not key:
                    continue
                label = str(p.get("label") or key)
                unit = str(p.get("unit") or "")
                lo = float(p.get("min", 0.0))
                hi = float(p.get("max", 1000.0))
                default = float(p.get("default", lo))
                row = self._runtime_table.rowCount()
                self._runtime_table.insertRow(row)
                self._runtime_table.setItem(row, 0, QTableWidgetItem(key))
                self._runtime_table.setItem(row, 1, QTableWidgetItem(label))
                box = QDoubleSpinBox()
                box.setRange(min(lo, hi), max(lo, hi))
                box.setDecimals(6)
                box.setValue(default)
                if unit:
                    box.setSuffix(f" {unit}")
                box.setToolTip(f"slot {p.get('slot', '?')}")
                self._runtime_table.setCellWidget(row, 2, box)
                self._runtime_spinboxes[key] = box

        # ── 生命周期 ──────────────────────────────────────

        def showEvent(self, event) -> None:
            super().showEvent(event)
            _win_dark_title(int(self.winId()))

        def closeEvent(self, event) -> None:
            if self._build_process and self._build_process.state() != QProcess.ProcessState.NotRunning:
                self._build_process.kill()
                self._build_process.waitForFinished(2000)
            self._close_serial()
            super().closeEvent(event)

    # ── 启动 GUI ──────────────────────────────────────────────

    app = QApplication(sys.argv)
    app.setApplicationName("YAML Config Builder")
    app.setOrganizationName("C-OOP")
    _apply_dark(app)

    window = _ConfigBuilderWindow()
    window.show()
    return app.exec()


# ═══════════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    if "--cli" in sys.argv:
        return run_cli()
    return _build_gui()


if __name__ == "__main__":
    sys.exit(main())
