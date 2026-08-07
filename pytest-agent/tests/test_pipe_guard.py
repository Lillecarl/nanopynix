from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest
from pytest_agent._pipe_guard import find_banned_pipe_reader

# Keeps a reader alive on stdin until the write end is closed, so the /proc
# scan has something to find. Nothing in the name or the arguments is a banned
# tool, so any detection has to come from where the test put it.
_BLOCK_ON_STDIN = "import sys; sys.stdin.read()"

# The interpreter binary itself, past any launcher that stands in front of it.
#
# `sys.executable` cannot serve, and the difference is the whole reason this
# constant exists. A pyproject.nix `mkVirtualEnv` -- what both this repository's
# dev shell and its CI gate give you -- puts an ELF launcher at
# `<venv>/bin/python`, and that launcher re-execs the base interpreter. An
# exec **replaces argv[0]**, so a child started as
# `Popen(["ugrep", ...], executable=sys.executable)` arrives in /proc calling
# itself `<venv>/bin/python3.14`, and the faked argv[0] the test below depends
# on is gone before the test can look at it.
#
# Measured, not assumed: with the venv launcher the child reports
# argv0='<venv>/bin/python3.14', and with this path it reports argv0='ugrep'.
#
# `/proc/self/exe` is the binary running this very process, which is what is
# left after every launcher has done its exec. Reading it needs no private
# attribute of CPython, and this module is procfs-only already.
_REAL_INTERPRETER = str(Path("/proc/self/exe").readlink())


def _settled(write_fd: int, *, expect_banned: bool) -> str | None:
    """find_banned_pipe_reader(write_fd), once the reader has been exec'd.

    The reader is a process the test just spawned, so there is a window where
    it exists but has not yet reached the state being asserted on. Poll until
    the answer is the expected shape, or until the deadline makes it a real
    failure rather than a slow one.
    """
    deadline = time.monotonic() + 2.0
    found = find_banned_pipe_reader(write_fd)
    while (found is None) == expect_banned and time.monotonic() < deadline:
        time.sleep(0.02)
        found = find_banned_pipe_reader(write_fd)
    return found


def _argv0_of(pid: int) -> str | None:
    """What process *pid* currently calls itself, for a failure message.

    Read once, and only after `_settled` has already waited, because a
    launcher that re-execs is exactly what this is meant to reveal and the
    re-exec has not happened yet in the first milliseconds of the child's
    life. Polling here would read the pre-exec argv[0] and report that the
    fixture is fine when it is not -- which is how this check failed the
    first time it was written.
    """
    try:
        first = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
    except OSError:
        return None
    return PurePosixPath(first.decode("utf-8", "replace")).name if first else None


def test_no_reader_reported_for_a_plain_python_reader() -> None:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen([sys.executable, "-c", _BLOCK_ON_STDIN], stdin=read_fd)
    os.close(read_fd)
    try:
        assert _settled(write_fd, expect_banned=False) is None
    finally:
        os.close(write_fd)
        proc.wait(timeout=5)


def test_detects_grep_as_the_pipe_reader() -> None:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(["grep", "x"], stdin=read_fd, stdout=subprocess.DEVNULL)
    os.close(read_fd)
    try:
        assert _settled(write_fd, expect_banned=True) == "grep"
    finally:
        os.close(write_fd)
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize("tool", ["ugrep", "rg", "ag"])
def test_detects_a_banned_tool_hidden_behind_a_wrapper_binary(tool: str) -> None:
    """The reported bypass: argv[0] is the tool, the running binary is not.

    Claude Code replaces `grep` with a shell function that runs
    `exec -a ugrep "$CLAUDE_CODE_EXECPATH"`, so a pipe an agent wrote as
    `| grep ...` shows up in /proc as comm=.claude-wrapped, argv[0]=ugrep. The
    guard matched on comm alone and waved it through -- the one caller it is
    built to stop was the one it could not see. Reproduced here without the
    harness: `executable=` sets what runs, args[0] sets what it calls itself.

    Parametrized over the greps that are not GNU's, because that is the other
    half of the same hole: an agent told to stop using `grep` reaches for `rg`
    next, and none of these were in the banned set. Running them all under a
    faked argv[0] means the test needs none of them installed.

    The launcher is `_REAL_INTERPRETER` rather than `sys.executable`; that
    constant says why, and the premise below is asserted rather than assumed
    because getting it wrong fails this test as `assert None is not None`,
    which names the guard instead of the fixture that broke.
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [tool, "-c", _BLOCK_ON_STDIN],
        executable=_REAL_INTERPRETER,
        stdin=read_fd,
    )
    os.close(read_fd)
    try:
        found = _settled(write_fd, expect_banned=True)
        # The diagnosis rides on the assertion rather than preceding it,
        # because a launcher that re-execs still shows the faked argv[0] for
        # the first few milliseconds of the child's life. Only a read taken
        # after the settle window above tells the truth.
        assert found is not None, (
            f"no banned reader found; the child's argv[0] is {_argv0_of(proc.pid)!r}. "
            f"If that is not {tool!r} then {_REAL_INTERPRETER} re-exec'd and replaced it, "
            "so this fixture is what broke, not the guard."
        )
        assert found.startswith(tool)
        # The refusal names both, or it tells an agent that wrote `grep` it
        # piped into a `ugrep` it has never heard of.
        assert "running as" in found
    finally:
        os.close(write_fd)
        proc.wait(timeout=5)


def test_detects_a_banned_tool_whose_argv0_has_been_made_innocuous() -> None:
    """The mirror image: argv[0] lies, the binary on disk does not.

    Trusting argv[0] alone would swap one blind spot for another, so comm and
    /proc/<pid>/exe are still checked.
    """
    real_grep = shutil.which("grep")
    if real_grep is None:
        pytest.skip("no grep on PATH to run under a different argv[0]")
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["totally-fine-tool", "x"],
        executable=real_grep,
        stdin=read_fd,
        stdout=subprocess.DEVNULL,
    )
    os.close(read_fd)
    try:
        assert _settled(write_fd, expect_banned=True) == "grep"
    finally:
        os.close(write_fd)
        proc.terminate()
        proc.wait(timeout=5)


def test_detects_a_banned_binary_reached_through_an_innocuous_symlink(tmp_path: Path) -> None:
    """Both names lie; only the binary on disk gives it away.

    The kernel takes `comm` from the basename of the path handed to execve, so
    invoking grep through a symlink named something else leaves argv[0] and
    comm *both* clean and /proc/<pid>/exe the only source still pointing at
    grep. Without this test, dropping `exe` from the checked sources passes the
    whole suite -- the other cases are each decided by argv[0] or comm.
    """
    real_grep = shutil.which("grep")
    if real_grep is None:
        pytest.skip("no grep on PATH to reach through a symlink")
    link = tmp_path / "innocuous"
    link.symlink_to(real_grep)

    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["also-harmless", "x"],
        executable=str(link),
        stdin=read_fd,
        stdout=subprocess.DEVNULL,
    )
    os.close(read_fd)
    try:
        found = _settled(write_fd, expect_banned=True)
        assert found is not None
        assert found.startswith("grep")
    finally:
        os.close(write_fd)
        proc.terminate()
        proc.wait(timeout=5)


def test_a_banned_name_in_a_later_argument_is_not_a_banned_reader() -> None:
    """Only argv[0] counts, not the rest of the command line.

    `pytest ... | tee grep.log` and `| some-tool --exclude awk` are both
    ordinary, and a guard that refuses them is a guard people turn off.
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-c", _BLOCK_ON_STDIN, "grep", "head", "awk"],
        stdin=read_fd,
    )
    os.close(read_fd)
    try:
        assert _settled(write_fd, expect_banned=False) is None
    finally:
        os.close(write_fd)
        proc.wait(timeout=5)


def test_no_reader_reported_for_a_regular_file_fd(tmp_path: Path) -> None:
    with (tmp_path / "out.txt").open("w", encoding="utf-8") as handle:
        assert find_banned_pipe_reader(handle.fileno()) is None
