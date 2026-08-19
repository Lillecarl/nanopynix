"""What the process was doing when a test hit its deadline.

Every async test runs under ``anyio.fail_after`` in ``tests/conftest.py``. When
that deadline fires, the test fails with a bare ``TimeoutError`` that says the
test took too long and nothing else. Five such failures in one CI job
(`#44 <https://github.com/Lillecarl/nanopynix/issues/44>`_) produced six lines
of output between them, so the only available next step was to run CI again.

This module builds the report that turns the next occurrence into evidence.
It answers the two questions a hang raises:

* **Which task is waiting, and where?** :func:`pending_tasks` walks the running
  loop and prints the suspended frames of every task except the one that timed
  out.
* **Is the thread that should answer still alive?** :func:`live_threads` lists
  every thread with its innermost Python frame. A Nix evaluator runs on a
  dedicated thread, and a subprocess reader runs on another, so "alive but
  parked in a read" and "gone" look completely different here and identical
  from a ``TimeoutError``.
* **What is a thread waiting on, below Python?** :func:`kernel_state` reads
  the state and the wait channel of every thread from ``/proc``. A thread that
  stops inside a native call shows its last Python frame and nothing more, and
  a stack of Nix frames needs a debugger and a permission that CI does not
  give. The wait channel needs neither, and it separates a file lock from a
  mutex and from a socket. Issue #211 is why it exists.

**The other tasks are the useful half, and they survive the unwind.** By the
time the wrapper catches ``TimeoutError``, ``fail_after`` has already cancelled
the test's own task, so the frames of the failing ``await`` are gone. A task
that a module-scoped or session-scoped fixture owns is outside that cancel
scope and is still parked exactly where it was. That is the shape #44
describes: a shared session stalls, and each test behind it pays the full
deadline.

This is observability and never an assertion. Every function here returns a
string, catches its own failures, and reports the failure as part of the text.
A diagnostic that raises would replace the hang with itself.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

# A parked task is usually two or three frames deep. Ten is enough to cross a
# few `async with` layers and short enough that twenty tasks stay readable.
STACK_FRAMES = 10


def _frame_lines(frames: list[object], indent: str) -> list[str]:
    lines: list[str] = []
    for frame in frames:
        code = getattr(frame, "f_code", None)
        if code is None or code.co_filename == __file__:
            # The report runs on the thread that timed out, so that thread's
            # innermost frames are this module building the report. Nobody
            # debugging a hang needs to read those.
            continue
        lines.append(f"{indent}{code.co_filename}:{frame.f_lineno} in {code.co_name}")  # type: ignore[attr-defined] -- guarded by the f_code check above
    return lines


def pending_tasks() -> str:
    """Every task still alive on the running loop, with where it is suspended.

    Skips the current task, whose frames the cancellation already unwound.
    """
    try:
        current = asyncio.current_task()
        alive = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    except RuntimeError as exc:
        return f"no running event loop, so no task state: {exc}"

    if not alive:
        return "no other task was alive; the hang is not a task waiting on another task"

    lines = [f"{len(alive)} task(s) still alive:"]
    for task in alive:
        lines.append(f"  - {task.get_name()}: {task.get_coro()!r}")
        try:
            lines.extend(_frame_lines(_await_chain(task.get_coro()), "      "))
        except Exception as exc:
            lines.append(f"      <no stack: {exc}>")
    return "\n".join(lines)


def _await_chain(coro: object) -> list[object]:
    """Every frame from ``coro`` down to whatever it is parked on.

    ``Task.get_stack`` is not enough. A suspended coroutine's frame has no
    ``f_back``, so that method returns the task's **outermost** frame and stops
    -- for a task a task group started, that is anyio's own runner and nothing
    of the caller's code. The awaited coroutines hang off ``cr_await`` instead,
    so this walks that chain. Measured: without it the report named
    ``anyio/_core/_tasks.py`` and no line of the test that was parked.
    """
    frames: list[object] = []
    seen: set[int] = set()
    current: object | None = coro
    while current is not None and len(frames) < STACK_FRAMES:
        if id(current) in seen:
            break
        seen.add(id(current))
        frame = _first_attr(current, ("cr_frame", "gi_frame", "ag_frame"))
        if frame is not None:
            frames.append(frame)
        # The chain ends at a non-coroutine awaitable -- a Future, a lock, an
        # event -- which has none of these and is where the task is waiting.
        current = _first_attr(current, ("cr_await", "gi_yieldfrom", "ag_await"))
    return frames


def _first_attr(obj: object, names: tuple[str, ...]) -> object | None:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def live_threads() -> str:
    """Every thread, with its innermost Python frames.

    A thread parked in a C call shows its last Python frame, which is what
    separates "the evaluator is still working" from "the evaluator is gone".
    """
    try:
        # `sys._current_frames()` is the only way to reach the frame of a
        # thread other than this one, and reading a parked thread's frame is
        # this module's whole subject.
        frames = sys._current_frames()  # type: ignore[reportPrivateUsage] -- no public API reaches another thread's frame  # noqa: SLF001 -- same reason
    except Exception as exc:
        return f"no thread state: {exc}"

    lines = [f"{threading.active_count()} thread(s):"]
    for thread in threading.enumerate():
        lines.append(f"  - {thread.name} (daemon={thread.daemon}, alive={thread.is_alive()})")
        frame = frames.get(thread.ident or -1)
        if frame is None:
            lines.append("      <no Python frame; in C or not started>")
            continue
        lines.extend(_frame_lines(_frames_of(frame), "      "))
    return "\n".join(lines)


def _frames_of(frame: object) -> list[object]:
    """The innermost :data:`STACK_FRAMES` frames of ``frame``'s stack, outermost first."""
    chain: list[object] = []
    current: object | None = frame
    while current is not None and len(chain) < STACK_FRAMES:
        chain.append(current)
        current = getattr(current, "f_back", None)
    return list(reversed(chain))


def kernel_state() -> str:
    """The state and the wait channel of every thread, from ``/proc``.

    ``stat`` gives one letter for the state of a thread: ``R`` runs, ``S``
    sleeps and takes a signal, ``D`` sleeps and takes none. ``wchan`` names the
    kernel function that a sleeping thread waits in. The pair separates the
    three things that a stopped build can be waiting for, and each one asks for
    a different correction:

    * ``flock_lock_file_wait`` -- a lock of the store, which another holder
      owns;
    * ``futex_wait`` -- a mutex or a condition variable inside the process;
    * ``sk_wait_data`` or a poll -- a socket, which is a substituter or a
      daemon.

    **This needs no privilege.** A native backtrace needs ptrace, and
    ``/proc/sys/kernel/yama/ptrace_scope`` is 1 on an ordinary machine, so a
    debugger reaches a descendant alone. A process always reads its own
    ``/proc/self/task``.

    The directory is Linux alone. macOS gets one line that says so, because a
    report that raises would replace the hang with itself.
    """
    tasks = Path("/proc/self/task")
    if not tasks.is_dir():
        return "no /proc/self/task on this platform, so no wait channels"

    lines = ["thread wait channels:"]
    try:
        entries = sorted(tasks.iterdir(), key=lambda entry: int(entry.name))
    except Exception as exc:
        return f"no wait channels: {exc}"

    for entry in entries:
        name = _read_proc(entry / "comm") or "?"
        wchan = _read_proc(entry / "wchan") or "?"
        # The state is the field after the command, and the command itself may
        # hold a space inside its parentheses, so read from the last `)`.
        stat = _read_proc(entry / "stat") or ""
        _, _, tail = stat.rpartition(") ")
        state = tail.split(" ")[0] if tail else "?"
        lines.append(f"  - {entry.name} {name}: state={state} wchan={wchan}")
    return "\n".join(lines)


def _read_proc(path: Path) -> str:
    """One line of ``/proc``, or an empty string when it cannot be read.

    A thread can end between the listing of the directory and this read, and a
    diagnostic must not fail for that.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def hang_report(seconds: float) -> str:
    """The whole report, for :meth:`BaseException.add_note`."""
    return "\n".join(
        (
            # `:g` and not `:.0f`: a run with NANOPYNIX_TEST_TIMEOUT below one
            # second reported "the 0s deadline", which reads as a bug in the
            # report rather than as the deadline the run asked for.
            f"--- state at the {seconds:g}s deadline (tests/support/hang_report.py, #44) ---",
            pending_tasks(),
            live_threads(),
            kernel_state(),
        )
    )
