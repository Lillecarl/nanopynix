from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# Options that make pytest print a listing and exit without running any test
# body. The guard's whole rationale -- "truncating hides the real failure" --
# does not apply to these: there are no failures to hide, and for
# --collect-only the interesting part (the collected count) is deliberately
# *last*, which makes `| tail -1` the right command rather than a mistake.
# Each entry is (option dest, the user-facing flag to name in messages).
_ZERO_DETAIL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("collectonly", "--collect-only"),
    ("showfixtures", "--fixtures"),
    ("show_fixtures_per_test", "--fixtures-per-test"),
    ("markers", "--markers"),
    ("setupplan", "--setup-plan"),
    ("help", "--help"),
    ("version", "--version"),
)


def zero_detail_mode(config: pytest.Config) -> str | None:
    """Return the listing-only flag active in *config*, or None.

    ``--setup-only`` is deliberately absent: unlike the flags above it really
    does execute fixtures, so a fixture error there is exactly the kind of
    detail the guard exists to protect.
    """
    for dest, flag in _ZERO_DETAIL_OPTIONS:
        # default= keeps this working if a builtin plugin declaring one of
        # these options is disabled via -p no:...; getoption would raise
        # ValueError for an unknown dest otherwise.
        if config.getoption(dest, default=False):
            return flag
    return None


# Common CLI tools that truncate or filter piped stdout. If pytest's own
# stdout is piped straight into one of these, the reader silently discards
# whatever pytest didn't print first (head/grep/sed/awk) or last (tail),
# which is exactly the failure mode this whole project exists to prevent --
# see CLAUDE.md's "Pytest output discipline" section for the human-authored
# version of this same rule.
_BANNED_READERS = frozenset(
    {"head", "tail", "grep", "egrep", "fgrep", "sed", "awk", "gawk", "mawk", "nawk"},
)


def find_banned_pipe_reader(fd: int = 1) -> str | None:  # noqa: C901 tracked complexity/arg-count debt, see TODO.md
    """Return the process name reading the other end of *fd*, if it is one of
    the common output-truncating tools; otherwise None.

    Detection works by matching the pipe's inode: *fd* is duplicated onto a
    pipe by the shell, and the reading process has that same pipe open on its
    own fd 0. We scan /proc for a process whose fd 0 refers to the same
    pipe. This only works on Linux (procfs); anywhere else this always
    returns None rather than guessing.
    """
    try:
        fd_stat = os.fstat(fd)
    except OSError:
        return None
    if not stat.S_ISFIFO(fd_stat.st_mode):
        return None

    proc_dir = Path("/proc")
    try:
        pid_entries = list(proc_dir.iterdir())
    except OSError:
        return None

    own_pid = os.getpid()
    for entry in pid_entries:
        if not entry.name.isdigit():
            continue
        if int(entry.name) == own_pid:
            continue
        try:
            reader_stat = (entry / "fd" / "0").stat()
        except OSError:
            continue
        if (reader_stat.st_dev, reader_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if comm in _BANNED_READERS:
            return comm
    return None
