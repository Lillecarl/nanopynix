from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_RUN_DIR_RE = re.compile(r"^runs-(\d+)$")


def existing_run_numbers(root: Path) -> list[int]:
    """Run numbers of the runs-NNNN directories under *root*, unsorted."""
    numbers: list[int] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        match = _RUN_DIR_RE.match(entry.name)
        if match is not None:
            numbers.append(int(match.group(1)))
    return numbers


def next_run_dir(root: Path) -> tuple[int, Path]:
    """Atomically claim the next sequential runs-N directory under *root*.

    Retries on collision so two pytest-agent invocations started against the
    same --agent-dir at the same moment don't clobber each other's run
    directory.
    """
    root.mkdir(parents=True, exist_ok=True)
    n = max(existing_run_numbers(root), default=0) + 1
    while True:
        candidate = root / f"runs-{n:04d}"
        try:
            candidate.mkdir()
        except FileExistsError:
            n += 1
            continue
        return n, candidate


def git_revision(cwd: Path) -> str | None:
    """Best-effort short git commit hash for *cwd*, or None if unavailable.

    pytest-agent shouldn't assume git is present or that the run happens
    inside a repo at all -- any failure here is silently treated as "no
    revision info," not an error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def append_run_record(history_path: Path, record: dict[str, Any]) -> None:
    with history_path.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record) + "\n")


def prune_old_runs(top_root: Path, keep: int, protect: Path) -> None:
    """Delete the oldest runs-* directories under top_root, keeping the newest
    *keep* (by run number) plus *protect* unconditionally.

    *protect* must be the run directory this very session just wrote to.
    Run numbers are assigned at session start (in next_run_dir), so under
    concurrent invocations against the same --agent-dir, a run that started
    later (and so has a higher number) can still finish -- and prune -- before
    this one does. Without an explicit protect argument, "keep the newest N
    by number" could delete *this* session's own just-written run if some
    other concurrent run happens to hold a higher number, even though this is
    the run whose data was only just durably recorded. *keep* is still
    clamped to at least 1 (via `effective_keep`'s floor below) so a
    misconfigured 0 or negative value can't be combined with a concurrent
    run to prune everything including *protect*.
    """
    numbered = sorted(
        ((n, top_root / f"runs-{n:04d}") for n in existing_run_numbers(top_root)),
        key=lambda pair: pair[0],
    )
    effective_keep = max(keep, 1)
    survivors = {run_dir for _, run_dir in numbered[-effective_keep:]}
    survivors.add(protect)
    for _, run_dir in numbered:
        if run_dir not in survivors:
            shutil.rmtree(run_dir, ignore_errors=True)
