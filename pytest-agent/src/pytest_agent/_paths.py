"""Rendering paths so that what pytest-agent prints can be pasted into a shell."""

from __future__ import annotations

import contextlib
import os
import shlex
from pathlib import Path


def display_path(path: Path, *, base: Path | None = None) -> str:
    """Render *path* for a human or agent to copy straight into a command.

    Whichever of the absolute and *base*-relative (current directory by
    default) forms is shorter, since both name the same file and the point is
    to be pasted. That covers the run directory sitting right under the
    project root (`.pytest-agent/runs-0001/...`) and equally an agent working
    two directories down, where `../../.pytest-agent/...` beats an absolute
    path that is mostly someone's home directory.

    Always shell-quoted: parametrized test ids put ``[`` and ``]`` in the
    file name, which fish refuses outright ("No matches for wildcard") and
    other shells may glob -- an unquoted path would recreate the exact
    copy-paste friction this printing exists to remove.
    """
    base = Path.cwd() if base is None else base
    candidates = [str(path)]
    # ValueError: on Windows, path and base on different drives have no
    # relative form at all -- then the absolute one is the only answer.
    with contextlib.suppress(ValueError):
        candidates.append(os.path.relpath(path, base))
    return shlex.quote(min(candidates, key=len))
