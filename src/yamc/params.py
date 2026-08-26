"""yamc/params — 静态调参纯逻辑（无 Qt 依赖）。

从 yaml_config_builder.py 顶层原样抽出，签名不变：
GUI「参数注入」页与 CLI（yamc_params / yamc_tune_static）共用，保证观感与行为一致。

能力全景（= GUI Tab1 交互模式的数据层）：
  变体发现   scan_configs（legacy conf/ + HardC Config/params/ 双布局）
  注入目标   find_project_root / find_target_file / _find_materialized_target
  状态检测   detect_current_config（读回「当前注入」config id）
  拍平/还原  flatten_config_tree / unflatten_config_tree（表格 ⇄ config 树）
  渲染       render_config_block（YAML 树 → C designated initializer）
  注入       inject_config（CONFIG BEGIN/END 区间写回，带 /* config: id */）
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

# ── 常量（与 GUI 原定义一致）────────────────────────────────────
BEGIN_MARKER = "/* CONFIG BEGIN */"
END_MARKER = "/* CONFIG END */"
CONFIG_ID_PATTERN = re.compile(r"/\*\s*config:\s*(.+?)\s*\*/")
C_IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9_]*$")

# 工具支持的 legacy 目标子目录（按优先级排列）
_APP_SUBDIRS = [
    ("User", "app"),
    ("User", "Application"),
]
# HardC 布局特征目录：Config/ + App/（拓扑/工程/参数 三层 YAML）
_HARDC_DIRS = ("Config", "App")


# ═══════════════════════════════════════════════════════════════
#  Project Discovery
# ═══════════════════════════════════════════════════════════════

def _first_app_dir(project_root: Path) -> Optional[Path]:
    """返回第一个存在的 User/app/ 或 User/Application/ 目录。"""
    for seg1, seg2 in _APP_SUBDIRS:
        d = project_root / seg1 / seg2
        if d.is_dir():
            return d
    return None


def _is_hardc_root(root: Path) -> bool:
    """判断是否为 HardC 仓库根（Config/ + App/ 同时存在）。"""
    return all((root / name).is_dir() for name in _HARDC_DIRS)


def _find_materialized_target(project_root: Path) -> Optional[Path]:
    """在 build/gen/<name>/ 下查找最近物化的 app_main.c（注入目标优先）。"""
    gen_dir = project_root / "build" / "gen"
    if not gen_dir.is_dir():
        return None
    candidates = [p for p in gen_dir.glob("*/app_main.c") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """从 start 向上搜索项目根（静态调参语义的工程根）。

    双模式识别：
    - legacy：含 conf/ 和 User/app/ 或 User/Application/
    - HardC：含 Config/ 和 App/
    """
    here = Path(start or os.getcwd()).resolve()
    for parent in [here] + list(here.parents):
        if (parent / "conf").is_dir() and _first_app_dir(parent) is not None:
            return parent
        if _is_hardc_root(parent):
            return parent
    return None


def find_target_file(project_root: Path) -> Optional[Path]:
    """查找注入目标文件。

    HardC 优先返回 build/gen/<name>/app_main.c（物化副本）；
    legacy 保持原有 User/app/ 或 User/Application/ 扫描逻辑。
    """
    if _is_hardc_root(project_root):
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
        lines.append(f"    .{key} = {rendered}{comma}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  变体发现 / 拍平 / 注入
# ═══════════════════════════════════════════════════════════════

def scan_configs(conf_dir: Path) -> list[dict]:
    """扫描目录下 *.yaml，返回 [{path, config_id, description, config}, ...]。

    兼容 legacy conf/*.yaml 与 HardC Config/params/*.yaml（schema 均为
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


def discover_params(root: Optional[Path] = None) -> dict:
    """一键定位注入工作区（= GUI _discover_project 的 CLI 版）。

    返回:
      {ok, root, layout: legacy|hardc, configs: [scan_configs 条目],
       target_file: Path|None, current_id: str|None}
    """
    project_root = find_project_root(root)
    if project_root is None:
        return {"ok": False, "reason": "未找到项目根目录（legacy: conf/ + User/app/；HardC: Config/ + App/）"}

    is_hardc = _is_hardc_root(project_root)
    if is_hardc:
        cfg_dir = project_root / "Config" / "params"
        layout = "hardc"
    else:
        cfg_dir = project_root / "conf"
        layout = "legacy"

    configs = scan_configs(cfg_dir)
    target = find_target_file(project_root)
    current_id = detect_current_config(target) if target is not None else None

    return {
        "ok": True,
        "root": project_root,
        "layout": layout,
        "configs": configs,
        "target_file": target,
        "current_id": current_id,
    }


def flatten_config_tree(node, prefix: str = "", out=None) -> list[tuple[str, object]]:
    """把嵌套 config dict 拍平成 [(dotted_key, value), ...]，叶子按 key.join('.') 排序。

    供可编辑参数表使用：行 = 参数名(点分路径) + 当前值。
    """
    if out is None:
        out = []
    for key, val in node.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            flatten_config_tree(val, dotted, out)
        else:
            out.append((dotted, val))
    return out


def unflatten_config_tree(flat: list[tuple[str, object]]) -> dict:
    """把 flatten_config_tree 的结果还原成嵌套 dict（点分 key 拆层）。"""
    root: dict = {}
    for dotted, val in flat:
        parts = dotted.split(".")
        cur = root
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = val
    return root


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


def write_variant_yaml(path: Path, config_id: str, description: str, config: dict) -> None:
    """把变体 YAML 写回磁盘（= GUI「保存到 YAML」）。"""
    doc = {
        "config_id": config_id,
        "description": description,
        "config": config,
    }
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )