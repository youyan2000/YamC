"""yamc/version — 版本自检（对标 libxr.PackageInfo）。

本地版本：安装包经 importlib.metadata 读取（未安装时退 package __version__）。
远端对比：GitHub latest release 最新 tag（best-effort，超时静默降级，不阻塞命令）。
"""

from __future__ import annotations

import logging
from typing import Optional

from . import __version__ as _pkg_version

LOG = logging.getLogger(__name__)

# 远端 release 来源（best-effort；yamc 独立工具仓库）
REMOTE_REPO = "YamC/yamc"


def local_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("yamc")
    except (ImportError, PackageNotFoundError):
        return _pkg_version


def remote_version(timeout: float = 3.0) -> Optional[str]:
    """查询 GitHub latest release tag；任何失败返回 None。"""
    try:
        import urllib.request
        url = f"https://api.github.com/repos/{REMOTE_REPO}/releases/latest"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            import json
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return str(data.get("tag_name") or "").lstrip("v") or None
    except Exception:
        return None


def check_and_print() -> None:
    """打印本地版本 + 远端新版本提示（对标 PackageInfo.check_and_print）。"""
    local = local_version()
    LOG.info(f"yamc {local}")
    remote = remote_version()
    if remote and remote != local:
        LOG.warning(f"yamc 有新版本可用: {remote} (当前: {local}) — 更新: pip install -U yamc")