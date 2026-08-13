"""A ruff configuration that ruff never reads is worse than no configuration.

ruff resolves the configuration for a file by walking up from that file and
taking the first directory that holds one. Inside that directory the order is
`.ruff.toml`, then `ruff.toml`, then the `[tool.ruff]` table of
`pyproject.toml`. The first one wins, and the others are never read.

`pynixd/` held both. Its `pyproject.toml` carried 103 lines of `[tool.ruff]`
-- 20 rule families, a `flake8-type-checking` section and three per-file-ignore
entries -- and `pynixd/ruff.toml` beside it selects four rule families. Every
measurement of that tree answered from the four, and the 103 lines read as the
rule while enforcing nothing.

The failure is silent in both directions: a rule added to the dead file never
fires, and a rule removed from it never stops firing.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests.support.suite_roots import REPO_ROOT

# Everything that ruff would read, in the order that ruff prefers them.
DEDICATED = (".ruff.toml", "ruff.toml")
IGNORED_PARTS = frozenset({".pytest-agent", ".git", "result", "node_modules", ".venv"})


def _directories_with_a_pyproject() -> list[Path]:
    return [path.parent for path in sorted(REPO_ROOT.rglob("pyproject.toml")) if not IGNORED_PARTS & set(path.parts)]


def test_no_directory_holds_a_ruff_config_that_ruff_ignores() -> None:
    """A `[tool.ruff]` beside a `ruff.toml` is dead, and it does not look dead."""
    shadowed: list[str] = []
    for directory in _directories_with_a_pyproject():
        dedicated = [name for name in DEDICATED if (directory / name).is_file()]
        if not dedicated:
            continue
        loaded = tomllib.loads((directory / "pyproject.toml").read_text())
        if "ruff" in loaded.get("tool", {}):
            relative = directory.relative_to(REPO_ROOT)
            shadowed.append(f"{relative}/pyproject.toml, shadowed by {relative}/{dedicated[0]}")
    assert not shadowed, (
        f"these `[tool.ruff]` tables are never read: {shadowed}. "
        "Move what they say into the file that wins, or delete them. Leaving both "
        "makes the repository state two rules and enforce one."
    )
