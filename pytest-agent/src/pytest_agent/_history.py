from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

_RUN_DIR_RE = re.compile(r"^runs-(\d+)$")

# Same file name pytest uses for the same purpose, so anyone who has looked
# inside /tmp/pytest-of-$USER recognizes it.
RUN_LOCK_NAME = ".lock"

# Written when the run claims its directory, not when it ends: a label's whole
# purpose is naming a run you intend to ask about later, and "later" starts
# while the run is still going. summary.json can't serve -- it appears at
# sessionfinish, so a 10-minute suite would be unfindable by name for 10
# minutes, and a killed one forever.
RUN_META_NAME = "meta.json"

# Labels name a run on the command line, so they may not need quoting, and may
# not be mistaken for the run number they sit beside in `--run`.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_LABEL_CHARS = 64

# How long a lock is believed. Long enough that no plausible suite outlives
# it (this repo's own is documented to run under `timeout 500`), short enough
# that a directory orphaned by a SIGKILL rejoins the pruning rotation the same
# day rather than accumulating one leaked run per hard kill.
RUN_LOCK_STALE_AFTER = 60 * 60 * 12


def validate_run_label(label: str) -> str:
    """Return *label* unchanged, or explain why it can't name a run.

    Rejecting a purely numeric label is the point of the check: `--run` takes
    either a number or a label, and a run labeled "123" would make
    `--run 123` mean two different runs depending on what else is on disk.
    Ruling it out at the writing end means the reading end never has to guess.
    """
    if not label:
        raise ValueError("a run label cannot be empty")
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"a run label is at most {MAX_LABEL_CHARS} characters, got {len(label)}")
    if label.isdigit():
        raise ValueError(f"{label!r} is all digits, which `--run` already reads as a run number")
    if _LABEL_RE.match(label) is None:
        raise ValueError(
            f"{label!r} is not a usable run label -- use letters, digits, '.', '_' and '-' "
            "(starting with a letter or digit)",
        )
    return label


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


def run_number_of(run_dir: Path) -> int | None:
    """The N in runs-NNNN, or None if *run_dir* isn't a run directory."""
    match = _RUN_DIR_RE.match(run_dir.name)
    return int(match.group(1)) if match is not None else None


def write_run_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    """Record what a run is, while it is still running.

    Written whole or not at all, because concurrent runs read this file: the
    prune path reads every run's meta on every sessionfinish, and a reader
    that caught a half-written file would see a run lose its label -- and its
    retention -- for the duration of a write.
    """
    temp_path = run_dir / f"{RUN_META_NAME}.tmp"
    temp_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(run_dir / RUN_META_NAME)


def read_run_meta(run_dir: Path) -> dict[str, Any] | None:
    """*run_dir*'s meta.json, or None if it hasn't got a readable one.

    Deliberately total: this is read while pruning, on every run's
    sessionfinish, across directories owned by other processes -- including
    one killed between claiming its directory and writing its meta. A raise
    here would turn one run's bad luck into every later run failing at
    sessionfinish, which is the shape of bug that wipes an archive.
    """
    try:
        raw = (run_dir / RUN_META_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        meta: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    return cast("dict[str, Any]", meta)


def run_label(run_dir: Path) -> str | None:
    """The label *run_dir* was started with, or None if it was unlabeled."""
    meta = read_run_meta(run_dir)
    if meta is None:
        return None
    label = meta.get("label")
    return label if isinstance(label, str) and label else None


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

    Labeled runs get their own budget of the same size, on top of the general
    one rather than carved out of it. A label is somebody saying "I intend to
    come back to this", and the case it exists for -- start a 10-minute suite
    in the background, then work through focused runs while it goes -- is
    exactly the case where that run falls out of the newest N before anyone
    asks about it. Additive so that the newest directory is still a survivor
    for any keep >= 1, which is what makes the claim-then-lock window in
    next_run_dir safe.
    """
    numbered = sorted(
        ((n, top_root / f"runs-{n:04d}") for n in existing_run_numbers(top_root)),
        key=lambda pair: pair[0],
    )
    effective_keep = max(keep, 1)
    survivors = {run_dir for _, run_dir in numbered[-effective_keep:]}
    labeled = [run_dir for _, run_dir in numbered if run_label(run_dir) is not None]
    survivors.update(labeled[-effective_keep:])
    survivors.add(protect)
    now = time.time()
    for _, run_dir in numbered:
        if run_dir not in survivors and not run_is_locked(run_dir, now):
            shutil.rmtree(run_dir, ignore_errors=True)
