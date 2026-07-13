from __future__ import annotations

import sys
from pathlib import Path


def prepare_sys_path() -> None:
    cwd = str(Path.cwd())
    sys.path[:] = [p for p in sys.path if p not in ("", ".", cwd)]
