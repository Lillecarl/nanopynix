from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_RUN_DIR_RE = re.compile(r"^runs-(\d+)$")

# Same file name pytest uses for the same purpose, so anyone who has looked
# inside /tmp/pytest-of-$USER recognizes it.
RUN_LOCK_NAME = ".lock"

# How long a lock is believed. Long enough that no plausible suite outlives
# it (this repo's own is documented to run under `timeout 500`), short enough
# that a directory orphaned by a SIGKILL rejoins the pruning rotation the same
# day rather than accumulating one leaked run per hard kill.
RUN_LOCK_STALE_AFTER = 60 * 60 * 12


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
        # After mkdir, not before -- there is nothing to lock until the
        # directory exists. The window between the two is not a race: a
        # freshly claimed directory always holds the highest number, and
        # prune_old_runs keeps the newest N for any N >= 1, so it survives
        # a concurrent prune landing in the gap on age alone.
        create_run_lock(candidate)
        return n, candidate


def create_run_lock(run_dir: Path) -> Path:
    """Mark *run_dir* as belonging to a session that is still running.

    Borrowed from how pytest guards its own numbered temp directories, down to
    the `.lock` name, because the hazard is the same one: pruning is "keep the
    newest N", and a concurrent run that started earlier holds a lower number,
    so finishing runs can delete a directory another session is still writing
    to. `protect` in prune_old_runs only ever covered the pruning session's
    own directory -- it cannot know about anyone else's.

    Only the convention is borrowed, not the code: pytest's helpers live in
    `_pytest.pathlib` with no public re-export, and the retention rules there
    are for ephemeral scratch dirs rather than for an archive that `history`
    and `compare` read back.
    """
    lock_path = run_dir / RUN_LOCK_NAME
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return lock_path


def release_run_lock(run_dir: Path) -> None:
    """Drop *run_dir*'s lock, making it prunable by later runs."""
    with contextlib.suppress(OSError):
        # Already gone, or on a filesystem that has since become unwritable.
        # Either way the staleness cutoff reclaims the directory eventually.
        (run_dir / RUN_LOCK_NAME).unlink()


def run_is_locked(run_dir: Path, now: float) -> bool:
    """Whether a live session still holds *run_dir*.

    A lock older than the cutoff is ignored rather than believed forever: a
    run killed with SIGKILL, or one that segfaulted, never gets to release
    its own, and a lock nobody can clear would exempt that directory from
    pruning for good.
    """
    try:
        held_since = (run_dir / RUN_LOCK_NAME).stat().st_mtime
    except OSError:
        return False
    return held_since > now - RUN_LOCK_STALE_AFTER


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

    A directory another session is still writing to is kept regardless of its
    number: *protect* covers only the pruning session's own run, and with a
    small --agent-keep-runs an older concurrent run sits outside the newest N
    while being very much alive. See create_run_lock.
    """
    numbered = sorted(
        ((n, top_root / f"runs-{n:04d}") for n in existing_run_numbers(top_root)),
        key=lambda pair: pair[0],
    )
    effective_keep = max(keep, 1)
    survivors = {run_dir for _, run_dir in numbered[-effective_keep:]}
    survivors.add(protect)
    now = time.time()
    for _, run_dir in numbered:
        if run_dir not in survivors and not run_is_locked(run_dir, now):
            shutil.rmtree(run_dir, ignore_errors=True)
