"""python -m yamc — 伞命令入口（等价 `yamc <tool> <action>`）。"""

import sys

from .cli import umbrella_main

if __name__ == "__main__":
    sys.exit(umbrella_main())