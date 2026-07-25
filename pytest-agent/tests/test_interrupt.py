# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile/makeconftest are typed as (*args, **kwargs) ->
# Path -- untyped varargs, not a stub gap specific to this file.

"""What a run leaves behind when it is killed, or when it stops making progress.

Every test here runs a real pytest subprocess and signals it, because that is
the only way to exercise the thing: an in-process session can't be sent a
SIGTERM without killing the session running the test.
"""

from __future__ import annotations

import json
import signal
import time
from typing import TYPE_CHECKING

from conftest import WAIT_TIMEOUT, running_pytest, wait_until
from pytest_agent._runtime import MAX_STUCK_DUMPS

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

HANGING_TEST = """
import time


def test_quick():
    assert True


def test_hangs():
    time.sleep(300)
"""


def _run_dir(project: Path) -> Path:
    return project / ".pytest-agent" / "runs-0001"


def test_a_run_killed_by_sigterm_still_reports_what_it_was_running(pytester: pytest.Pytester) -> None:
    # `timeout 500 pytest tests` is the documented way to run this repo's
    # suite, and it ends a hung run with SIGTERM. Left to its default action
    # that kills the process outright -- so the one run that most needed
    # explaining would be the only one leaving nothing behind.
    pytester.makepyfile(test_hang=HANGING_TEST)
    index_path = _run_dir(pytester.path) / "index.jsonl"

    with running_pytest(pytester.path, "--agent-heartbeat", "0.2") as proc:
        wait_until(
            lambda: index_path.is_file() and "test_quick" in index_path.read_text(encoding="utf-8"),
            "the first test to finish",
        )
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=WAIT_TIMEOUT)

    assert "interrupted by SIGTERM while running: test_hang.py::test_hangs" in out
    # The summary block a normal run ends with is still printed: a killed run
    # degrades to a shorter answer rather than to no answer.
    assert "done in " in out
    assert "full detail:" in out

    summary = json.loads((_run_dir(pytester.path) / "summary.json").read_text(encoding="utf-8"))
    assert summary["interrupted_at"] == "test_hang.py::test_hangs"
    assert summary["killed_by"] == "SIGTERM"
    # The test that did finish before the kill is still counted and recorded.
    assert summary["counts"]["passed"] == 1

    history = (pytester.path / ".pytest-agent" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 1
    assert json.loads(history[0])["interrupted_at"] == "test_hang.py::test_hangs"


def test_a_second_sigterm_does_not_leave_a_half_written_summary(pytester: pytest.Pytester) -> None:
    # The handler restores the default action before raising, so a second
    # signal -- `timeout --kill-after`, or an impatient caller -- kills the
    # process instead of re-entering a sessionfinish that is mid-write.
    # summary.json is written to a temp file and renamed for the same reason.
    pytester.makepyfile(test_hang=HANGING_TEST)
    index_path = _run_dir(pytester.path) / "index.jsonl"

    with running_pytest(pytester.path, "--agent-heartbeat", "0.2") as proc:
        wait_until(
            lambda: index_path.is_file() and "test_quick" in index_path.read_text(encoding="utf-8"),
            "the first test to finish",
        )
        proc.send_signal(signal.SIGTERM)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=WAIT_TIMEOUT)

    summary_path = _run_dir(pytester.path) / "summary.json"
    if summary_path.is_file():
        # Whether it got that far is a race, but if it exists it is whole:
        # a truncated summary.json reads as corruption rather than as a run
        # that was killed early.
        assert json.loads(summary_path.read_text(encoding="utf-8"))["run"] == 1


def test_a_projects_own_sigterm_handler_is_not_taken_over(pytester: pytest.Pytester) -> None:
    # A suite that installs its own SIGTERM handler means it; quietly
    # replacing it would change the behaviour of the thing under test.
    pytester.makeconftest(
        """
        import signal
        import sys
        from pathlib import Path


        def _handler(signum, frame):
            Path("custom-handler-ran").write_text("yes")
            sys.exit(3)


        signal.signal(signal.SIGTERM, _handler)
        """,
    )
    pytester.makepyfile(test_hang=HANGING_TEST)
    index_path = _run_dir(pytester.path) / "index.jsonl"

    with running_pytest(pytester.path, "--agent-heartbeat", "0.2") as proc:
        wait_until(
            lambda: index_path.is_file() and "test_quick" in index_path.read_text(encoding="utf-8"),
            "the first test to finish",
        )
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=WAIT_TIMEOUT)

    assert (pytester.path / "custom-handler-ran").is_file()
    assert "interrupted by SIGTERM" not in out


def test_a_test_that_runs_too_long_has_its_stack_dumped_while_it_is_still_hung(
    pytester: pytest.Pytester,
) -> None:
    # The half of this that works when the signal handler can't: a thread
    # wedged in a C call never reaches the interpreter loop, so it never runs
    # a Python handler on the way out. Dumping while it hangs gets the stack
    # onto disk before anything tries to kill it.
    pytester.makepyfile(
        test_slow="""
        import time


        def test_slow_one():
            time.sleep(300)
        """,
    )
    stuck_path = _run_dir(pytester.path) / "test_slow.py" / "test_slow_one.stuck.txt"

    with running_pytest(pytester.path, "--agent-heartbeat", "0.1", "--agent-stuck-after", "0.5") as proc:
        wait_until(stuck_path.is_file, "a stack dump for the hung test")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=WAIT_TIMEOUT)

    dumped = stuck_path.read_text(encoding="utf-8")
    assert "still running after" in dumped
    assert "test_slow_one" in dumped

    # "still running", not "stuck": from out here a slow test and a hung one
    # are the same thing, and only the stacks tell them apart.
    assert "still running after" in out
    assert "stack dumped to" in out
    # And the end-of-run block points back at it, so the two halves of a
    # killed hang -- what it was running, and what it was doing -- are one
    # read apart rather than one turn apart.
    assert "its stack, dumped while it ran:" in out


def test_the_stack_of_a_hung_test_stops_being_dumped_after_a_few_tries(pytester: pytest.Pytester) -> None:
    # Enough dumps to tell a wedge (identical stacks) from slow progress (the
    # stack moves), then silence: a run left going overnight must not grow an
    # unbounded file, and the terminal line is not worth repeating forever.
    pytester.makepyfile(
        test_slow="""
        import time


        def test_slow_one():
            time.sleep(300)
        """,
    )
    stuck_path = _run_dir(pytester.path) / "test_slow.py" / "test_slow_one.stuck.txt"

    def dumps() -> int:
        return stuck_path.read_text(encoding="utf-8").count("=== still running after") if stuck_path.is_file() else 0

    with running_pytest(pytester.path, "--agent-heartbeat", "0.1", "--agent-stuck-after", "0.3") as proc:
        wait_until(lambda: dumps() >= MAX_STUCK_DUMPS, f"{MAX_STUCK_DUMPS} stack dumps")
        # Well past when a sixth would have been written, had anything been
        # going to write one.
        time.sleep(1.0)
        assert dumps() == MAX_STUCK_DUMPS
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=WAIT_TIMEOUT)


def test_stuck_dumps_can_be_turned_off(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_slow="""
        import time


        def test_slow_one():
            time.sleep(1.5)
        """,
    )

    with running_pytest(pytester.path, "--agent-heartbeat", "0.1", "--agent-stuck-after", "0") as proc:
        out, _ = proc.communicate(timeout=WAIT_TIMEOUT)

    assert "still running after" not in out
    assert not (_run_dir(pytester.path) / "test_slow.py" / "test_slow_one.stuck.txt").exists()
    assert "1 passed" in out
