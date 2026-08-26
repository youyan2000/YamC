"""pytest 环境: 把 yamc 仓库的 src/ 加进 sys.path，使 `yamc` 包可导入（src-layout）。"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"  # F:\My_Projects\HardC\yamc\src
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))