# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path --
# untyped varargs, not a stub gap specific to this file.

"""What `pytest-agent watch` reports, and when.

Two halves, on purpose.

The larger half drives `poll_once` against a run directory the test writes by
hand. Every event this command produces is then reachable, including the ones
that are hard to stage against a real pytest: a process killed at the wrong
moment, a half-written record, a corrupt line, two hundred failures. A watcher
whose only test is a healthy run is a watcher whose failure paths have never
run -- and its failure paths are the whole reason it exists.

The smaller half runs a real `pytest --agent` subprocess and watches it, which
is what proves the hand-written directories above resemble the real thing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import WAIT_TIMEOUT, run_cli, running_pytest, wait_until
from pytest_agent._records import QueryError
from pytest_agent._watch import (
    EXIT_CLEAN,
    EXIT_RUN_DIED,
    EXIT_RUN_FAILED,
    MAX_REPORTED_FAILURES,
    MAX_STUCK_NOTICES,
    STATUS_GRACE,
    WatchOptions,
    WatchState,
    poll_once,
    watch,
)

# A clock the tests hand to poll_once, so nothing here sleeps to make time
# pass. Every "the test has been running for N seconds" case is then a
# subtraction rather than a wait.
NOW = 1_000_000.0

FAILING_AND_PASSING = """
def test_passes():
    assert True


def test_fails():
    assert 1 == 2, "on purpose"
"""


def _make_run(root: Path, *, number: int = 1, label: str | None = None, pid: int | None = None) -> Path:
    """A run directory holding just what a live run has written by its start."""
    run_dir = root / f"runs-{number:04d}"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run": number,
                "run_dir": run_dir.name,
                "label": label,
                "pid": os.getpid() if pid is None else pid,
            },
        ),
        encoding="utf-8",
    )
    return run_dir


def _append_record(run_dir: Path, nodeid: str, outcome: str, message: str | None = None) -> None:
    record: dict[str, Any] = {"nodeid": nodeid, "outcome": outcome}
    if message is not None:
        record["crash"] = {"message": message, "path": "test_x.py", "lineno": 3}
    with (run_dir / "index.jsonl").open("a", encoding="utf-8") as index:
        index.write(json.dumps(record) + "\n")


def _write_status(
    run_dir: Path,
    *,
    running: str | None = None,
    running_since_s: float = 0.0,
    written_at: float = NOW,
    distributed: bool = False,
) -> None:
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "run": 1,
                "written_at": written_at,
                "elapsed_s": running_since_s,
                "running": running,
                "running_since_s": running_since_s,
                "counts": {},
                "total_collected": 0,
                "distributed": distributed,
            },
        ),
        encoding="utf-8",
    )


def _write_summary(run_dir: Path, **counts: int) -> None:
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run": 1,
                "run_dir": run_dir.name,
                "duration_s": 12.5,
                "exit_status": 1 if any(counts.get(name) for name in ("failed", "error")) else 0,
                "counts": {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "collect_error": 0, **counts},
            },
        ),
        encoding="utf-8",
    )


def _dead_pid() -> int:
    """A pid belonging to nothing.

    A real process, started and reaped, rather than a number picked out of the
    air: Linux hands out pids in increasing order up to `pid_max`, so a
    just-reaped one will not be reused while a test finishes, and any number
    invented here might belong to something.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait(timeout=WAIT_TIMEOUT)
    return proc.pid


def _events(run_dir: Path, state: WatchState, *, now: float = NOW, stuck_after: float = 120.0) -> list[str]:
    return poll_once(run_dir, state, now, WatchOptions(stuck_after=stuck_after))


# --- finished tests -------------------------------------------------------


def test_a_failing_record_is_reported_with_its_cause(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    _append_record(run_dir, "test_x.py::test_bad", "failed", "AssertionError: 1 != 2")

    events = _events(run_dir, WatchState())

    assert events == ["FAIL  test_x.py::test_bad -- AssertionError: 1 != 2"]


def test_a_passing_record_is_not_an_event(tmp_path: Path) -> None:
    # The command exists to interrupt somebody. A test that passed is not a
    # reason to.
    run_dir = _make_run(tmp_path)
    _append_record(run_dir, "test_x.py::test_good", "passed")

    assert _events(run_dir, WatchState()) == []


@pytest.mark.parametrize(
    ("outcome", "label"),
    [("failed", "FAIL "), ("error", "ERROR"), ("collect_error", "ERROR")],
)
def test_each_failing_outcome_gets_its_label(tmp_path: Path, outcome: str, label: str) -> None:
    # `error` and `collect_error` share one label: both mean the test never
    # got to say anything about itself, and that is the distinction that
    # decides whether to go and read it.
    run_dir = _make_run(tmp_path)
    _append_record(run_dir, "test_x.py::test_bad", outcome, "boom")

    assert _events(run_dir, WatchState())[0].startswith(f"{label} ")


def test_a_record_is_reported_once_and_not_again(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    state = WatchState()
    _append_record(run_dir, "test_x.py::test_bad", "failed", "boom")

    first = _events(run_dir, state)
    second = _events(run_dir, state)

    assert len(first) == 1
    assert second == []


def test_a_half_written_record_waits_for_the_rest_of_itself(tmp_path: Path) -> None:
    """The run appends as each test finishes, so the last line is often partial.

    Parsing it would report a failure that has not happened, or drop one that
    has; keeping the fragment until its newline arrives does neither.
    """
    run_dir = _make_run(tmp_path)
    state = WatchState()
    complete = json.dumps({"nodeid": "test_x.py::test_bad", "outcome": "failed"})
    with (run_dir / "index.jsonl").open("a", encoding="utf-8") as index:
        index.write(complete[: len(complete) // 2])

    assert _events(run_dir, state) == []

    with (run_dir / "index.jsonl").open("a", encoding="utf-8") as index:
        index.write(complete[len(complete) // 2 :] + "\n")

    assert _events(run_dir, state) == ["FAIL  test_x.py::test_bad -- no crash message in this record"]


def test_a_complete_line_that_is_not_json_is_reported_once_and_skipped(tmp_path: Path) -> None:
    # Corruption, unlike a fragment, is worth saying out loud -- but once, not
    # on every poll, or a damaged file would bury the run it describes.
    run_dir = _make_run(tmp_path)
    state = WatchState()
    with (run_dir / "index.jsonl").open("a", encoding="utf-8") as index:
        index.write("{not json at all\n")
        index.write(json.dumps({"nodeid": "test_x.py::test_bad", "outcome": "failed"}) + "\n")

    events = _events(run_dir, state)

    assert sum(line.startswith("WARN") for line in events) == 1
    assert any(line.startswith("FAIL") for line in events)

    with (run_dir / "index.jsonl").open("a", encoding="utf-8") as index:
        index.write("{also broken\n")

    assert _events(run_dir, state) == []


def test_the_failure_flood_is_capped_so_the_end_of_the_run_survives(tmp_path: Path) -> None:
    """A suite with hundreds of failures must not cost the DONE line.

    An agent harness stops a watch that produces too many notifications, and
    the last line is the one carrying the totals.
    """
    run_dir = _make_run(tmp_path)
    state = WatchState()
    for index in range(MAX_REPORTED_FAILURES + 25):
        _append_record(run_dir, f"test_x.py::test_{index}", "failed", "boom")

    events = _events(run_dir, state)

    assert sum(line.startswith("FAIL") for line in events) == MAX_REPORTED_FAILURES
    assert sum(line.startswith("MORE") for line in events) == 1
    assert state.failures_seen == MAX_REPORTED_FAILURES + 25

    _append_record(run_dir, "test_x.py::test_last", "failed", "boom")
    # One MORE line for the whole run, not one per poll.
    assert _events(run_dir, state) == []

    _write_summary(run_dir, failed=state.failures_seen)
    done = _events(run_dir, state)
    assert done[-1].startswith("DONE ")
    assert f"{MAX_REPORTED_FAILURES + 26} failed" in done[-1]


# --- a test that will not finish ------------------------------------------


def test_a_test_past_the_threshold_is_reported_stuck(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    _write_status(run_dir, running="test_x.py::test_hang", running_since_s=130.0)

    events = _events(run_dir, WatchState(), stuck_after=120.0)

    assert events == ["STUCK 130s test_x.py::test_hang"]


def test_the_age_of_a_running_test_grows_with_the_age_of_the_file(tmp_path: Path) -> None:
    """`running_since_s` is an age at write time, not a timestamp.

    monotonic clocks have no shared origin, so a start time would mean nothing
    in this process. The reader adds however long ago the file was written.
    """
    run_dir = _make_run(tmp_path)
    _write_status(run_dir, running="test_x.py::test_hang", running_since_s=100.0, written_at=NOW - 50.0)

    events = _events(run_dir, WatchState(), now=NOW, stuck_after=120.0)

    assert events == ["STUCK 150s test_x.py::test_hang"]


def test_a_stuck_test_is_reported_at_each_doubling_and_then_stops(tmp_path: Path) -> None:
    # Once at the threshold, then 2x, 4x, 8x. A wedged test says so and then
    # stays quiet, instead of filing a notification every two seconds for as
    # long as the run lasts.
    run_dir = _make_run(tmp_path)
    state = WatchState()
    reported: list[str] = []
    for age in (130.0, 260.0, 520.0, 1040.0, 2080.0, 4160.0):
        _write_status(run_dir, running="test_x.py::test_hang", running_since_s=age)
        reported.extend(_events(run_dir, state, stuck_after=120.0))

    assert len(reported) == MAX_STUCK_NOTICES
    assert state.stuck_notices["test_x.py::test_hang"] == MAX_STUCK_NOTICES


def test_a_stuck_line_names_the_stack_dump_when_the_run_wrote_one(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    dump = run_dir / "test_x.py" / "test_hang.stuck.txt"
    dump.parent.mkdir(parents=True)
    dump.write_text("a stack\n", encoding="utf-8")
    _write_status(run_dir, running="test_x.py::test_hang", running_since_s=130.0)

    assert "stack: " in _events(run_dir, WatchState(), stuck_after=120.0)[0]


def test_a_distributed_run_reports_no_stuck_test(tmp_path: Path) -> None:
    # Under xdist many tests run at once and `running` names an arbitrary one,
    # so its age measures nothing. A notice would be a guess dressed up as an
    # observation.
    run_dir = _make_run(tmp_path)
    _write_status(run_dir, running="test_x.py::test_hang", running_since_s=900.0, distributed=True)

    assert _events(run_dir, WatchState(), stuck_after=120.0) == []


def test_a_zero_threshold_reports_no_stuck_test(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    _write_status(run_dir, running="test_x.py::test_hang", running_since_s=9000.0)

    assert _events(run_dir, WatchState(), stuck_after=0.0) == []


# --- the end of the run ---------------------------------------------------


def test_a_summary_ends_the_watch_and_carries_the_counts(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, label="nightly")
    state = WatchState()
    _write_summary(run_dir, passed=835, failed=4, skipped=10)

    events = _events(run_dir, state)

    assert state.ended
    assert state.exit_code == EXIT_RUN_FAILED
    assert events == [
        (
            "DONE  runs-0001 [nightly]: 4 failed, 835 passed, 10 skipped in 12s (exit 1) "
            "-- pytest-agent digest --run nightly"
        ),
    ]


def test_a_clean_run_ends_clean(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    state = WatchState()
    _write_summary(run_dir, passed=42)

    events = _events(run_dir, state)

    assert state.exit_code == EXIT_CLEAN
    # No pointer at `digest`, because there is nothing for it to group.
    assert "digest" not in events[0]


def test_a_run_that_exits_badly_with_no_failing_test_is_still_a_failure(tmp_path: Path) -> None:
    # No tests collected, a usage error, an interrupt. The caller asked how the
    # run went, and "0 failures" would not be an answer to that.
    run_dir = _make_run(tmp_path)
    (run_dir / "summary.json").write_text(
        json.dumps({"exit_status": 4, "duration_s": 1.0, "counts": {"passed": 0}}),
        encoding="utf-8",
    )
    state = WatchState()

    _events(run_dir, state)

    assert state.exit_code == EXIT_RUN_FAILED


def test_a_failure_is_reported_before_the_finish_that_counts_it(tmp_path: Path) -> None:
    # Both become true between two polls when a run ends quickly. Reporting
    # the finish first would announce four failures and then name one of them
    # afterwards.
    run_dir = _make_run(tmp_path)
    _append_record(run_dir, "test_x.py::test_bad", "failed", "boom")
    _write_summary(run_dir, failed=1)

    events = _events(run_dir, WatchState())

    assert [line.split()[0] for line in events] == ["FAIL", "DONE"]


# --- the run that stopped without finishing -------------------------------


def test_a_dead_process_that_wrote_no_summary_is_reported(tmp_path: Path) -> None:
    """The event that makes silence mean something.

    Without it a segfault, an OOM kill and a healthy run all look the same
    from out here: no output, indefinitely.
    """
    run_dir = _make_run(tmp_path, label="bg1", pid=_dead_pid())
    _append_record(run_dir, "test_x.py::test_bad", "failed", "boom")
    _write_status(run_dir, running="test_x.py::test_hang", running_since_s=5.0)
    state = WatchState()

    events = _events(run_dir, state)

    assert state.ended
    assert state.exit_code == EXIT_RUN_DIED
    died = events[-1]
    assert died.startswith("DIED  runs-0001 [bg1]:")
    assert "1 failures seen so far" in died
    assert "last running test_x.py::test_hang" in died


def test_a_process_that_finished_before_the_poll_is_not_reported_dead(tmp_path: Path) -> None:
    # A short run exits the instant it is done, so its pid is already gone the
    # first time this looks. It finished; saying it died would be wrong.
    run_dir = _make_run(tmp_path, pid=_dead_pid())
    _write_summary(run_dir, passed=3)
    state = WatchState()

    events = _events(run_dir, state)

    assert not state.died
    assert events[-1].startswith("DONE ")


def test_a_live_process_is_not_reported_dead(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)  # this test's own pid, which is very much alive
    _write_status(run_dir, running="test_x.py::test_slow", running_since_s=1.0)

    assert _events(run_dir, WatchState()) == []


def test_a_run_with_no_pid_is_judged_by_the_age_of_its_status(tmp_path: Path) -> None:
    """The fallback, for a run whose meta.json never got written.

    It cannot be the primary test: a run started with
    --agent-status-interval 0 writes that file once and never again, and
    calling such a run dead thirty seconds in would be wrong every time.
    """
    run_dir = tmp_path / "runs-0001"
    run_dir.mkdir(parents=True)
    _write_status(run_dir, running="test_x.py::test_slow", written_at=NOW - STATUS_GRACE - 1.0)

    state = WatchState()
    _events(run_dir, state)

    assert state.died


def test_a_run_with_no_pid_and_a_fresh_status_is_alive(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs-0001"
    run_dir.mkdir(parents=True)
    _write_status(run_dir, running="test_x.py::test_slow", written_at=NOW - 1.0)

    state = WatchState()
    _events(run_dir, state)

    assert not state.died


# --- attaching ------------------------------------------------------------


def test_watch_gives_up_and_says_what_it_waited_for(tmp_path: Path) -> None:
    (tmp_path / ".pytest-agent").mkdir()

    with pytest.raises(QueryError, match="nightly"):
        watch(str(tmp_path / ".pytest-agent"), "nightly", WatchOptions(wait=0.0, poll=0.01))


def test_watch_waits_for_an_agent_directory_that_does_not_exist_yet(tmp_path: Path) -> None:
    """The first run of a fresh checkout creates that directory.

    Resolving it once, up front, refused exactly the case this command is for
    -- arming a watcher before the run it watches. Found by doing that.
    """
    with pytest.raises(QueryError, match="waited"):
        watch(str(tmp_path / "nothing-here"), None, WatchOptions(wait=0.0, poll=0.01))


def test_watch_ignores_a_run_that_had_already_finished(tmp_path: Path) -> None:
    """Attaching to the newest run is the trap this default avoids.

    The newest run is right for every other subcommand, because they answer
    about a run that is over. Here it would report the *previous* suite as
    finished, immediately, which is a wrong answer shaped exactly like a right
    one.
    """
    agent_dir = tmp_path / ".pytest-agent"
    finished = _make_run(agent_dir)
    _write_summary(finished, passed=1)

    with pytest.raises(QueryError, match="no run was in progress"):
        watch(str(agent_dir), None, WatchOptions(wait=0.0, poll=0.01))


# --- against a real run ---------------------------------------------------


def test_a_real_run_is_followed_from_its_failure_to_its_end(pytester: pytest.Pytester) -> None:
    """The half that proves the hand-written directories above are realistic.

    A live `pytest --agent` writes meta.json, index.jsonl, status.json and
    summary.json itself, so this fails if any of those shapes drifts from what
    poll_once expects.
    """
    pytester.makepyfile(test_live=FAILING_AND_PASSING)
    run_dir = pytester.path / ".pytest-agent" / "runs-0001"

    with running_pytest(pytester.path, "--agent-label", "live") as proc:
        wait_until(run_dir.is_dir, "the run to claim its directory")
        result = run_cli(
            ["watch", "--run", "live", "--dir", str(pytester.path / ".pytest-agent")],
            cwd=pytester.path,
        )
        proc.communicate(timeout=WAIT_TIMEOUT)

    lines = result.stdout.splitlines()
    assert any(line.startswith("FAIL ") and "test_fails" in line for line in lines), result.stdout
    assert lines[-1].startswith("DONE "), result.stdout
    assert "1 failed, 1 passed" in lines[-1], result.stdout
    assert result.returncode == EXIT_RUN_FAILED
    # The attach line is on stderr rather than stdout, so that it reaches a
    # log without becoming an event: the events of this command are things
    # that happened to the run, and this is a thing that happened to the
    # watcher.
    assert "watching" in result.stderr
    assert "watching" not in result.stdout


def test_a_watcher_armed_before_the_run_attaches_when_it_starts(pytester: pytest.Pytester) -> None:
    """`--wait` is what makes "start it, then watch it" a sequence, not a race.

    The run claims its directory inside pytest_configure, which is after the
    interpreter, the plugins and every conftest have loaded -- so the watcher
    routinely gets there first.
    """
    pytester.makepyfile(test_live=FAILING_AND_PASSING)

    started_at = time.monotonic()
    with running_pytest(pytester.path, "--agent-label", "later") as proc:
        result = run_cli(
            ["watch", "--run", "later", "--dir", str(pytester.path / ".pytest-agent"), "--poll", "0.1"],
            cwd=pytester.path,
        )
        proc.communicate(timeout=WAIT_TIMEOUT)

    assert result.returncode == EXIT_RUN_FAILED, result.stderr
    assert result.stdout.splitlines()[-1].startswith("DONE "), result.stdout
    assert time.monotonic() - started_at < WAIT_TIMEOUT
