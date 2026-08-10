"""The package that the graph installs as an editable.

**Nothing here reaches the store.** The graph builds a PEP 660 wheel from a
copy of this tree, and then rewrites the path that the wheel records, so the
environment reads the tree that `DDRN_EDITABLE_ROOT` names at run time.
`ddrn/examples/venv-graph/scripts/build-editable.py` does the rewrite.
"""

from __future__ import annotations

WHICH_TREE = "the tree that the check derivation named"


def greet() -> str:
    return f"hello from {WHICH_TREE}"


def main() -> None:
    print(greet())
