"""The deadline report says something useful, and never raises.

``tests/support/hang_report.py`` runs at exactly one moment: an async test hit
its deadline and is already failing. Two failure modes would make it worse than
absent. A report that raises replaces the hang with itself, and the real
failure is lost. A report that returns nothing looks like "there was nothing to
see", which is the false all-clear that #44 is about.

Both directions are pinned here. The report must name a task that is genuinely
parked, and it must also say plainly when there is nothing parked -- an empty
string would read as the second failure mode.
"""

from __future__ import annotations

import fcntl
import tempfile
import threading
import time
from pathlib import Path

import anyio
import pytest

from test_support import deadline, hang_report as hang_report_module
from test_support.hang_report import hang_report, kernel_state, live_threads, open_descriptors, pending_tasks

_MARKER = "hang_report_parked_here"


async def _parked(started: anyio.Event) -> None:
    """A task that waits forever, under a name this file can search for."""
    started.set()
    await anyio.sleep_forever()


async def test_pending_tasks_names_a_parked_task_and_its_frame() -> None:
    """The half that matters in CI: what is waiting, and where it stopped."""
    report = ""
    async with anyio.create_task_group() as group:
        started = anyio.Event()
        group.start_soon(_parked, started, name=_MARKER)
        await started.wait()

        report = pending_tasks()
        group.cancel_scope.cancel()

    assert _MARKER in report, f"the parked task is missing from the report:\n{report}"
    assert "task(s) still alive" in report
    assert "support/hang_report.py" not in report, "the report should show the parked frames, not its own"
    # The whole await chain, not just the task's outermost frame. `_parked` is
    # the caller's code and `sleep` is where it actually stopped; a report with
    # only anyio's runner frame in it would name neither.
    assert "test_hang_report.py" in report, f"no frame from the parked coroutine:\n{report}"
    assert "in _parked" in report, f"the report does not reach the caller's own frame:\n{report}"
    assert "in sleep" in report, f"the report does not reach what the task is waiting on:\n{report}"


async def test_pending_tasks_says_so_when_nothing_is_parked() -> None:
    """Silence is the failure mode. An empty report must not look like a clean one.

    The current task is skipped on purpose -- ``fail_after`` has already
    unwound it by the time the wrapper builds this -- so a test with no other
    task alive is the normal case, and it needs a sentence rather than nothing.
    """
    report = pending_tasks()
    assert report.strip(), "an empty report reads as 'nothing was wrong'"
    assert "no other task was alive" in report or "task(s) still alive" in report


def test_pending_tasks_survives_having_no_event_loop() -> None:
    """A diagnostic that raises replaces the failure it was meant to explain."""
    report = pending_tasks()
    assert "no running event loop" in report


def test_live_threads_names_a_running_thread() -> None:
    """#44 asks whether the evaluator thread is alive. This is that answer."""
    running = threading.Event()
    release = threading.Event()

    def _wait() -> None:
        running.set()
        release.wait(timeout=30)

    thread = threading.Thread(target=_wait, name="hang-report-probe", daemon=True)
    thread.start()
    try:
        running.wait(timeout=30)
        report = live_threads()
    finally:
        release.set()
        thread.join(timeout=30)

    assert "hang-report-probe" in report, f"a live thread is missing:\n{report}"
    assert "alive=True" in report
    assert "test_hang_report.py" in report, f"no Python frame for any thread:\n{report}"


def test_hang_report_joins_both_halves_and_names_the_deadline() -> None:
    report = hang_report(120)
    assert "120s deadline" in report
    assert "thread(s):" in report
    assert "#44" in report, "the reader needs somewhere to go with this"


def test_the_deadline_keeps_its_sub_second_value() -> None:
    """A short ``NANOPYNIX_TEST_TIMEOUT`` printed "the 0s deadline".

    That reads as a broken report rather than as the deadline the run asked
    for, and a short deadline is exactly what someone reproducing a hang sets.
    """
    assert "0.4s deadline" in hang_report(0.4)


def test_the_report_leaves_out_its_own_frames() -> None:
    """The report runs on the thread that timed out, so it is in that stack.

    Measured before this check: the ``MainThread`` entry ended with two frames
    of ``hang_report.py`` itself, which is noise at the exact moment a reader
    is scanning for the frame that matters.
    """
    frame_lines = [line for line in hang_report(120).splitlines() if line.startswith("      ")]
    assert frame_lines, "no frame lines at all; the check below would pass by matching nothing"
    offenders = [line for line in frame_lines if "support/hang_report.py" in line]
    assert not offenders, "the report shows its own frames:\n" + "\n".join(offenders)


async def _never_answers() -> None:
    await anyio.sleep_forever()


async def test_the_deadline_wrapper_attaches_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, over the real wrapper that each suite's conftest installs.

    ``with_test_timeout`` is what the collection hook wraps every async test
    in, so this drives that function rather than rebuilding its shape. A
    rebuild would keep passing if the wrapper stopped attaching the note.

    The deadline is patched down instead of waited out: the real one is 120
    seconds, and this test must not take that long to prove one `add_note`.
    Patching the module attribute works because the wrapper reads it when it
    runs, not when it is built.
    """
    monkeypatch.setattr(deadline, "ASYNC_TEST_TIMEOUT", 0.05)
    wrapped = deadline.with_test_timeout(_never_answers)

    with pytest.raises(TimeoutError) as caught:
        await wrapped()

    notes = getattr(caught.value, "__notes__", [])
    assert notes, "the TimeoutError carries no note, so CI would show the bare timeout again"
    assert "0.05s deadline" in notes[0], f"the wrapper did not pass its own deadline through:\n{notes[0]}"
    assert "thread(s):" in notes[0]


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="the wait channel of a thread is a Linux file")
def test_the_report_names_the_kernel_wait_of_a_blocked_thread() -> None:
    """A thread that waits for a file lock is what issue #211 has to tell apart.

    `flock` is the deterministic way to park a thread in the kernel, and it is
    also the wait that the store of Nix uses, so this asserts the case the
    report exists for. The report must name the channel and not the address:
    a running thread answers `0`, and a report of `0` for every thread would
    be the false all-clear that this file already guards against.
    """
    with tempfile.NamedTemporaryFile() as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with Path(holder.name).open("r+", encoding="utf-8") as waiter:

            def _wait_for_the_lock() -> None:
                fcntl.flock(waiter.fileno(), fcntl.LOCK_EX)

            blocked = threading.Thread(target=_wait_for_the_lock, daemon=True)
            blocked.start()
            try:
                deadline = time.monotonic() + 5
                report = ""
                while time.monotonic() < deadline:
                    report = kernel_state()
                    if "lock" in report:
                        break
                    time.sleep(0.05)
                assert "thread wait channels:" in report
                assert "lock" in report, report
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                blocked.join(timeout=5)


def test_the_report_says_so_where_proc_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS has no `/proc`, and the report must say that rather than raise."""

    def _never_a_directory(_self: Path) -> bool:
        return False

    monkeypatch.setattr(Path, "is_dir", _never_a_directory)
    assert "no /proc/self/task" in kernel_state()


def test_the_report_counts_the_open_descriptors() -> None:
    """The count and the highest number, because 1024 breaks `select`.

    Issue #271 was a `ValueError: filedescriptor out of range in select()`
    that reached no log, and the report said nothing that would have named it.
    """
    text = hang_report(1.0)
    assert "open descriptors:" in text


def test_the_report_warns_when_a_descriptor_is_outside_an_fd_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A descriptor at or above `FD_SETSIZE` is called out, and not just counted."""
    monkeypatch.setattr(hang_report_module, "_FD_SETSIZE", 3)
    line = open_descriptors()
    assert "at or above FD_SETSIZE" in line, line


def test_the_count_says_so_when_the_platform_has_no_proc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """macOS has no `/proc/self/fd`, and a report that raised would hide the hang."""

    def _absent(_name: str) -> Path:
        return tmp_path / "absent"

    monkeypatch.setattr(hang_report_module, "Path", _absent)
    assert "no /proc/self/fd" in open_descriptors()
