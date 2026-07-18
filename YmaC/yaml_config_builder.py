#!/usr/bin/env python3
"""
YAML Config Builder — 通用 YAML → C 代码配置注入工具
======================================================
自动发现项目结构，扫描 conf/*.yaml 配置变体，
将选中的 YAML 配置渲染为 C designated initializer 并注入 User/app/app_main.c。

跨平台：Windows / Debian Linux
原生暗黑模式：跟随系统主题，Windows 暗色标题栏

依赖：pip install PyQt6 pyyaml darkdetect

用法：
    python tools/yaml_config_builder.py          # 启动 GUI
    python tools/yaml_config_builder.py --cli    # 命令行模式（列出配置）
"""

from __future__ import annotations

import ctypes
import os
import re
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

# 工具支持的目标子目录（按优先级排列）
_APP_SUBDIRS = [
    ("User", "app"),
    ("User", "Application"),
]


def _first_app_dir(project_root: Path) -> Optional[Path]:
    """返回第一个存在的 User/app/ 或 User/Application/ 目录。"""
    for seg1, seg2 in _APP_SUBDIRS:
        d = project_root / seg1 / seg2
        if d.is_dir():
            return d
    return None


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """从 start 向上搜索含 conf/ 和 User/app/ 或 User/Application/ 的项目根。"""
    here = Path(start or os.getcwd()).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "conf").is_dir() and _first_app_dir(parent) is not None:
            return parent
    return None


def find_target_file(project_root: Path) -> Optional[Path]:
    """在 User/app/ 或 User/Application/ 下搜索含 CONFIG BEGIN/END 标记的 .c 文件。

    优先 User/app/，其次 User/Application/。
    优先返回 app_main.c，否则扫描目录中第一个含标记的 .c 文件。
    """
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
    """扫描 conf/*.yaml，返回 [{path, config_id, description, config}, ...]。

    排除以 _example 或 _template 结尾的文件。
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

    updated = original[:begin_idx] + new_block + original[end_idx + len(END_MARKER):]
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
    """命令行模式：列出配置、应用配置。"""
    root = find_project_root()
    if root is None:
        print("错误：未找到项目根目录（需要 **conf/** 和 **User/app/ 或 User/Application/**）")
        return 1

    target = find_target_file(root)
    if target is None:
        print("错误：User/app/ 或 User/Application/ 中未找到含 CONFIG 标记的 .c 文件")
        return 1

    print(f"项目根目录: {root}")
    print(f"目标文件:   {target.relative_to(root)}")
    cfgs = scan_configs(root / "conf")
    if not cfgs:
        print("conf/ 中未找到有效的 YAML 配置文件")
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
        from PyQt6.QtCore import QProcess, Qt
        from PyQt6.QtGui import QColor, QFont, QPalette
        from PyQt6.QtWidgets import (
            QApplication,
            QGroupBox,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QSplitter,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        print(f"PyQt6 未安装。请执行: pip install PyQt6 darkdetect\n详情: {exc}")
        return 1

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

    class _ConfigBuilderWindow(QMainWindow):
        """YAML Config Builder 主窗口。"""

        _project_root: Optional[Path]
        _configs: list[dict]
        _current_id: Optional[str]
        _build_info: Optional[dict]
        _build_process: Optional[QProcess]

        def __init__(self) -> None:
            super().__init__()
            self._project_root = None
            self._configs = []
            self._current_id = None
            self._build_info = None
            self._build_process = None

            self.setWindowTitle("YAML Config Builder")
            self.setMinimumSize(960, 620)
            self.resize(1100, 700)
            self._init_ui()
            self._discover_project()

        def _init_ui(self) -> None:
            central = QWidget()
            self.setCentralWidget(central)
            root_layout = QVBoxLayout(central)
            root_layout.setContentsMargins(12, 12, 12, 12)
            root_layout.setSpacing(8)

            # 状态栏
            status_row = QHBoxLayout()
            self._lbl_project = QLabel("项目: (未检测)")
            self._lbl_project.setStyleSheet("font-weight: bold; font-size: 14px;")
            status_row.addWidget(self._lbl_project)
            status_row.addStretch()
            self._lbl_current = QLabel("当前注入: —")
            self._lbl_current.setStyleSheet("color: #4ec9b0; font-size: 13px;")
            status_row.addWidget(self._lbl_current)
            root_layout.addLayout(status_row)

            # 主分割器
            splitter = QSplitter(Qt.Orientation.Horizontal)

            # 左：配置列表
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            lbl_list = QLabel("配置变体 (conf/*.yaml)")
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
            root_layout.addWidget(splitter, stretch=1)

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
            root_layout.addLayout(btn_row)

            # 日志
            log_group = QGroupBox("日志")
            log_inner = QVBoxLayout(log_group)
            self._log = QPlainTextEdit()
            self._log.setReadOnly(True)
            self._log.setMaximumBlockCount(2000)
            self._log.setFont(mono)
            self._log.setMinimumHeight(100)
            log_inner.addWidget(self._log)
            root_layout.addWidget(log_group)

        # ── 项目发现 ──────────────────────────────────────

        def _discover_project(self) -> None:
            root = find_project_root()
            if root is None:
                self._log_msg("⚠ 未找到项目根目录（需要 **conf/** 和 **User/app/ 或 User/Application/**）")
                self._lbl_project.setText("项目: 未找到")
                return

            self._project_root = root
            self._lbl_project.setText(f"项目: {root.name}  ({root})")

            conf_dir = root / "conf"
            self._configs = scan_configs(conf_dir)
            self._log_msg(f"扫描 conf/ → {len(self._configs)} 个配置变体")

            target = find_target_file(root)
            if target is None:
                self._log_msg("⚠ User/app/ 或 User/Application/ 中未找到含 CONFIG 标记的 .c 文件")
                return

            self._target_file = target
            self._current_id = detect_current_config(target)
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

            self._btn_apply.setEnabled(len(self._configs) > 0)

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

            target = getattr(self, '_target_file', None) or self._project_root / "User" / "app" / "app_main.c"
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
            self._log_msg(f"$ {' '.join(cmd)}")
            self._btn_build.setText("⏹ 取消编译")
            self._btn_build.setStyleSheet(
                "QPushButton { background-color: #c42b1c; color: #fff;"
                "border: none; padding: 8px 20px; border-radius: 4px;"
                "font-weight: bold; font-size: 13px; }"
                "QPushButton:hover { background-color: #e04337; }"
            )

            self._build_process = QProcess(self)
            self._build_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self._build_process.readyReadStandardOutput.connect(self._on_build_output)
            self._build_process.finished.connect(self._on_build_finished)
            self._build_process.errorOccurred.connect(self._on_build_error)
            self._build_process.setWorkingDirectory(str(self._project_root or os.getcwd()))
            self._build_process.start(cmd[0], cmd[1:])

        def _kill_build(self) -> None:
            if self._build_process is None:
                return
            self._log_msg("⚠ 用户取消编译")
            self._build_process.kill()
            self._build_process.waitForFinished(3000)
            self._build_process = None
            self._reset_build_button()

        def _reset_build_button(self) -> None:
            self._btn_build.setEnabled(True)
            self._btn_build.setText("⚙ 编译")
            self._btn_build.setStyleSheet("")

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

        def _on_build_finished(self, exit_code: int, _exit_status) -> None:
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

        # ── 生命周期 ──────────────────────────────────────

        def showEvent(self, event) -> None:
            super().showEvent(event)
            _win_dark_title(int(self.winId()))

        def closeEvent(self, event) -> None:
            if self._build_process and self._build_process.state() != QProcess.ProcessState.NotRunning:
                self._build_process.kill()
                self._build_process.waitForFinished(2000)
            super().closeEvent(event)

    # ── 启动 GUI ──────────────────────────────────────────────

    app = QApplication(sys.argv)
    app.setApplicationName("YAML Config Builder")
    app.setOrganizationName("WEILAI_SuperCap")
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
