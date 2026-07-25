from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

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
#
# The grep family is listed by every name it ships under, not just GNU's:
# an agent told to stop using `grep` will reach for `rg` next, and `rg` is on
# PATH in this very environment.
#
# `wc` is deliberately absent, for the same reason `tee` is: it destroys the
# output wholesale rather than selectively, so asking for one is an explicit
# choice to want a count, not an attempt to read output through a keyhole.
_BANNED_READERS = frozenset(
    {
        "head",
        "tail",
        "cut",
        "sed",
        "awk",
        "gawk",
        "mawk",
        "nawk",
        "grep",
        "egrep",
        "fgrep",
        "ugrep",
        "rg",
        "ripgrep",
        "ag",
        "ack",
        "ack-grep",
        "pcregrep",
        "pcre2grep",
    },
)


def _basename(value: str) -> str:
    """Last path component of *value*, with a deleted-binary marker removed.

    /proc/<pid>/exe reads back as "/path/to/grep (deleted)" once the binary
    has been replaced or garbage-collected out from under a running process,
    which on a Nix system is an ordinary thing to happen mid-session.
    """
    return PurePosixPath(value.removesuffix(" (deleted)")).name


class ReaderIdentity(NamedTuple):
    """What a process reading our pipe calls itself, from three procfs sources.

    Kept as three named fields rather than one list because which source a
    name came from is exactly what the refusal message needs to explain
    itself -- see banned_name().
    """

    argv0: str | None
    comm: str | None
    exe: str | None

    def banned_name(self) -> str | None:
        """The banned tool this process is, described so the caller recognizes it.

        Checked argv[0]-first: it is the only source that survives a wrapper
        with the caller's intent intact.

        Naming only the match would tell an agent it piped into `ugrep` when
        it wrote `grep`, which reads as the tool being wrong about what just
        happened. Naming the running executable alongside it is what makes
        the refusal explicable: something substituted one for the other.
        """
        for name in (self.argv0, self.comm, self.exe):
            if name is not None and name in _BANNED_READERS:
                if self.comm is not None and self.comm != name:
                    return f"{name} (running as {self.comm})"
                return name
        return None


def read_identity(entry: Path) -> ReaderIdentity:
    """Every name process *entry* can reasonably be called.

    One name is not enough, because a process's identity is recorded in three
    places that a wrapper can disagree with each other:

    - ``cmdline``'s argv[0] -- what the caller *meant*. `exec -a ugrep claude`
      and busybox's applet dispatch both put the real tool here while `comm`
      says something else.
    - ``comm`` -- the executable actually running, truncated to 15 bytes.
    - ``exe`` -- the binary on disk, which is what is left when argv[0] has
      been faked to something innocuous.

    Checking only ``comm`` is what let the reported bypass through: Claude
    Code replaces `grep` with a shell function running
    `exec -a ugrep "$CLAUDE_CODE_EXECPATH"`, so a pipe an agent wrote as
    `| grep ...` appears in /proc as comm=.claude-wrapped, argv[0]=ugrep. The
    guard exists to stop exactly that agent, and the agent's own harness was
    defeating it.

    Every read is separately guarded: a process can exit between the scan and
    the read, and /proc/<pid>/exe on a foreign uid is EACCES. Agent mode must
    never be the reason a test run fails, and this runs in
    pytest_cmdline_main, before anything else has happened.
    """
    argv0: str | None = None
    comm: str | None = None
    exe: str | None = None
    with contextlib.suppress(OSError):
        first = (entry / "cmdline").read_bytes().split(b"\0", 1)[0].decode("utf-8", "replace")
        # Empty for a kernel thread, and for a process that exited between the
        # /proc scan and this read.
        if first:
            argv0 = _basename(first)
    with contextlib.suppress(OSError):
        comm = (entry / "comm").read_text(encoding="utf-8").strip() or None
    with contextlib.suppress(OSError):
        exe = _basename(str((entry / "exe").readlink()))
    return ReaderIdentity(argv0=argv0, comm=comm, exe=exe)


def find_banned_pipe_reader(fd: int = 1) -> str | None:
    """Return the name of the process reading the other end of *fd*, if it is
    one of the common output-truncating tools; otherwise None.

    Detection works by matching the pipe's inode: *fd* is duplicated onto a
    pipe by the shell, and the reading process has that same pipe open on its
    own fd 0. We scan /proc for a process whose fd 0 refers to the same
    pipe. This only works on Linux (procfs); anywhere else this always
    returns None rather than guessing.

    Only the *immediate* reader is inspected. `pytest | cat | grep x` is
    therefore not caught -- see TODO.md, where that gap is recorded rather
    than left to be discovered again.
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
        banned = read_identity(entry).banned_name()
        if banned is not None:
            return banned
    return None
