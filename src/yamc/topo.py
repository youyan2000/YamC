"""yamc/topo — 拓扑选择（list / show）。

数据源：hardc 库根 Config/topologies/*.yaml（status: ready 才可生成）。
复用 engine.ensure_hardc_dir 的库根四级定位（env/--hardc-path/../hardc/submodule）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .engine import ensure_hardc_dir


def resolve_hardc(root: Optional[Path] = None,
                  hardc_path: Optional[Path] = None) -> Optional[Path]:
    """解析 hardc 库根（只读，不做 git 操作）。

    返回 None 表示未找到（调用方给出用户可见错误）。
    """
    try:
        return ensure_hardc_dir(
            Path(root or Path.cwd()).resolve(),
            lambda level, msg: None,
            no_submodule=True,
            hardc_path=hardc_path,
        )
    except Exception:
        return None


def _topo_dir(hardc: Path) -> Path:
    return hardc / "Config" / "topologies"


def list_topologies(hardc: Path) -> list[dict]:
    """列出库根全部拓扑（含 status/description/params 数）。"""
    out: list[dict] = []
    d = _topo_dir(hardc)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        params = data.get("params") or []
        out.append({
            "name": str(data.get("name") or p.stem),
            "path": p,
            "display": str(data.get("display") or data.get("name") or p.stem),
            "status": str(data.get("status") or "planned"),
            "description": str(data.get("description") or ""),
            "params_count": len(params) if isinstance(params, list) else 0,
            "data": data,
        })
    return out


def show_topology(hardc: Path, name: str) -> Optional[dict]:
    """按名取拓扑（name 或文件名匹配），不存在返回 None。"""
    for t in list_topologies(hardc):
        if t["name"] == name or t["path"].stem == name:
            return t
    return None