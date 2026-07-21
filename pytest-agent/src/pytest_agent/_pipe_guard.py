from __future__ import annotations

import os
import stat
from pathlib import Path

# Common CLI tools that truncate or filter piped stdout. If pytest's own
# stdout is piped straight into one of these, the reader silently discards
# whatever pytest didn't print first (head/grep/sed/awk) or last (tail),
# which is exactly the failure mode this whole project exists to prevent --
# see CLAUDE.md's "Pytest output discipline" section for the human-authored
# version of this same rule.
_BANNED_READERS = frozenset(
    {"head", "tail", "grep", "egrep", "fgrep", "sed", "awk", "gawk", "mawk", "nawk"}
)


def find_banned_pipe_reader(fd: int = 1) -> str | None:
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
