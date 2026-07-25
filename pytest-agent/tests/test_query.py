from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pytest_agent._history import create_run_lock, write_run_meta
from pytest_agent._query import run

if TYPE_CHECKING:
    from pathlib import Path


def _record(nodeid: str, outcome: str, **extra: Any) -> dict[str, Any]:
    file_part, _, test_part = nodeid.partition("::")
    return {
        "nodeid": nodeid,
        "outcome": outcome,
        "duration_s": 0.1,
        "log_file": f"{file_part}/{test_part}.log",
        "has_traceback": outcome != "passed",
        **extra,
    }


def _crash(message: str, path: str = "tests/test_a.py", lineno: int = 10) -> dict[str, Any]:
    exc_type, _, _rest = message.partition(":")
    return {"crash": {"exc_type": exc_type, "message": message, "path": path, "lineno": lineno}}


def _write_run(agent_dir: Path, run_number: int, records: list[dict[str, Any]]) -> Path:
    run_dir = agent_dir / f"runs-{run_number:04d}"
    run_dir.mkdir(parents=True)
    with (run_dir / "index.jsonl").open("w", encoding="utf-8") as index_file:
        for record in records:
            index_file.write(json.dumps(record) + "\n")
            log_path = run_dir / record["log_file"]
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"nodeid: {record['nodeid']}\noutcome: {record['outcome']}\n\nthe detail of {record['nodeid']}\n",
                encoding="utf-8",
            )
    return run_dir


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with one recorded run, made the current directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    _write_run(
        tmp_path / ".pytest-agent",
        1,
        [
            _record("tests/test_a.py::test_ok", "passed"),
            _record(
                "tests/test_a.py::test_hover[in_process-local]",
                "failed",
                **_crash("FileNotFoundError: /nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-swagger.json"),
            ),
            _record(
                "tests/test_b.py::test_hover[daemon-local]",
                "failed",
                **_crash("FileNotFoundError: /nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-swagger.json"),
            ),
            _record("tests/test_b.py::test_other", "error", **_crash("ValueError: unrelated")),
        ],
    )
    return tmp_path


def test_show_finds_a_test_by_a_substring_of_its_nodeid(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["show", "test_ok"]) == 0

    out = capsys.readouterr().out
    assert "tests/test_a.py::test_ok" in out
    assert "the detail of tests/test_a.py::test_ok" in out


def test_show_prints_a_copy_pasteable_quoted_path_for_a_parametrized_id(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del project
    assert run(["show", "test_hover[in_process"]) == 0

    out = capsys.readouterr().out
    # The brackets must be quoted: fish refuses an unquoted `[...]` path
    # outright, which is exactly the friction this command removes.
    assert "'.pytest-agent/runs-0001/tests/test_a.py/test_hover[in_process-local].log'" in out


def test_show_refuses_an_ambiguous_pattern_and_lists_the_candidates(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del project
    assert run(["show", "test_hover"]) == 1

    out = capsys.readouterr().out
    assert "2 tests" in out
    assert "tests/test_a.py::test_hover[in_process-local]" in out
    assert "tests/test_b.py::test_hover[daemon-local]" in out


def test_show_all_prints_every_match(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["show", "test_hover", "--all"]) == 0

    out = capsys.readouterr().out
    assert "the detail of tests/test_a.py::test_hover[in_process-local]" in out
    assert "the detail of tests/test_b.py::test_hover[daemon-local]" in out


def test_show_reports_a_miss_without_a_traceback(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["show", "no_such_test"]) == 1

    assert "no test matching 'no_such_test'" in capsys.readouterr().err


def test_last_failures_lists_each_failure_with_its_log_path(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["last-failures"]) == 0

    out = capsys.readouterr().out
    assert "3 failed/errored of 4 recorded" in out
    # The log path *is* the nodeid with :: written as a separator, so only
    # the path is printed. No quoting for a path that doesn't need it --
    # only ids with shell metacharacters (the parametrized case below).
    assert "\n.pytest-agent/runs-0001/tests/test_b.py/test_other.log\n" in out
    assert "tests/test_b.py::test_other" not in out
    assert "ValueError: unrelated" in out
    assert "test_ok" not in out


def test_last_failures_names_the_nodeid_when_the_path_cannot_be_read_back_as_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A `/` inside a parametrized id is sanitized to `_` on disk, so this
    # path alone does not say which test it belongs to.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    run_dir = (tmp_path / ".pytest-agent" / "runs-0001").resolve()
    run_dir.mkdir(parents=True)
    record = {
        "nodeid": "tests/test_a.py::test_p[a/b]",
        "outcome": "failed",
        "duration_s": 0.1,
        "log_file": "tests/test_a.py/test_p[a_b].log",
        "has_traceback": True,
    }
    (run_dir / "index.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert run(["last-failures"]) == 0

    out = capsys.readouterr().out
    assert "'.pytest-agent/runs-0001/tests/test_a.py/test_p[a_b].log'  (tests/test_a.py::test_p[a/b])" in out


def test_last_failures_detail_inlines_the_logs(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["last-failures", "--detail"]) == 0

    out = capsys.readouterr().out
    assert "the detail of tests/test_b.py::test_other" in out


def test_digest_collapses_one_root_cause_across_tests(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["digest"]) == 0

    out = capsys.readouterr().out
    # The two swagger failures differ only by store hash, so they are one
    # cause; the ValueError is a second.
    assert "2 distinct root causes" in out
    assert "2x  FileNotFoundError: /nix/store/" in out
    assert "1x  ValueError: unrelated" in out


def test_digest_reports_frames_in_first_party_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    _write_run(
        tmp_path / ".pytest-agent",
        1,
        [
            _record(
                "tests/test_a.py::test_x",
                "failed",
                **_crash("ValueError: bad"),
                frames=[
                    {"path": ".venv/lib/site-packages/requests/api.py", "lineno": 61, "first_party": False},
                    {"path": "src/pkg/thing.py", "lineno": 44, "first_party": True},
                ],
            ),
        ],
    )

    assert run(["digest"]) == 0

    out = capsys.readouterr().out
    assert "first-party frames" in out
    assert "src/pkg/thing.py:44" in out
    assert "requests/api.py" not in out
    # The crash site differs from the innermost first-party frame here, so
    # it is worth its own line.
    assert "-> raised in tests/test_a.py:10" in out


def test_digest_does_not_repeat_the_crash_site_that_is_already_the_last_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    _write_run(
        tmp_path / ".pytest-agent",
        1,
        [
            _record(
                "tests/test_a.py::test_x",
                "failed",
                **_crash("AssertionError: nope", path="tests/test_a.py", lineno=7),
                frames=[{"path": "tests/test_a.py", "lineno": 7, "first_party": True}],
            ),
        ],
    )

    assert run(["digest"]) == 0

    out = capsys.readouterr().out
    assert out.count("tests/test_a.py:7") == 1
    assert "raised" not in out


def test_queries_degrade_gracefully_on_a_run_written_before_crash_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Up to --agent-keep-runs old run directories survive an upgrade, and
    # theirs have no crash/frames fields at all.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    _write_run(tmp_path / ".pytest-agent", 1, [_record("tests/test_a.py::test_old", "failed")])

    assert run(["digest"]) == 0

    out = capsys.readouterr().out
    assert "no crash info recorded" in out
    assert "tests/test_a.py::test_old" in out


def test_the_newest_run_is_queried_by_default_and_run_selects_an_older_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    agent_dir = tmp_path / ".pytest-agent"
    _write_run(agent_dir, 1, [_record("tests/test_a.py::test_old", "failed")])
    _write_run(agent_dir, 2, [_record("tests/test_a.py::test_new", "failed")])

    assert run(["last-failures"]) == 0
    assert "test_new" in capsys.readouterr().out

    assert run(["last-failures", "--run", "1"]) == 0
    assert "test_old" in capsys.readouterr().out


def test_a_run_directory_with_no_index_is_not_treated_as_the_newest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # next_run_dir claims runs-NNNN at session start, so a run that died
    # before writing anything leaves an empty directory behind. Falling back
    # to the newest *usable* run keeps queries working right afterwards.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    agent_dir = tmp_path / ".pytest-agent"
    _write_run(agent_dir, 1, [_record("tests/test_a.py::test_real", "failed")])
    (agent_dir / "runs-0002").mkdir()

    assert run(["last-failures"]) == 0
    assert "test_real" in capsys.readouterr().out


def test_the_agent_dir_is_found_from_a_subdirectory_with_paths_relative_to_it(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subdir = project / "src" / "deep"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    assert run(["last-failures"]) == 0

    out = capsys.readouterr().out
    assert "3 failed/errored" in out
    # `../../.pytest-agent/...` beats an absolute path that is mostly
    # somebody's home directory, and works from right here.
    assert "../../.pytest-agent/runs-0001/tests/test_b.py/test_other.log" in out
    assert str(project) not in out


def test_a_truncated_final_index_line_does_not_break_a_query(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # index.jsonl is appended to as tests finish, so a killed run can leave
    # a half-written last line. Everything before it is still good data.
    index_path = project / ".pytest-agent" / "runs-0001" / "index.jsonl"
    with index_path.open("a", encoding="utf-8") as index_file:
        index_file.write('{"nodeid": "tests/test_c.py::test_tr')

    assert run(["last-failures"]) == 0
    assert "3 failed/errored of 4 recorded" in capsys.readouterr().out


def test_a_corrupt_index_line_that_is_not_the_last_one_is_an_error(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Only a truncated *final* line is explainable by a killed run. Damage in
    # the middle means records after it are missing too, and quietly skipping
    # the line would print a confident failure count that is simply wrong.
    index_path = project / ".pytest-agent" / "runs-0001" / "index.jsonl"
    lines = index_path.read_text(encoding="utf-8").splitlines()
    lines[1] = '{"nodeid": "tests/test_b.py::test'
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert run(["last-failures"]) == 1
    assert "is corrupt: line 2 is not valid JSON" in capsys.readouterr().err


@pytest.fixture
def three_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three runs of the same two tests, with one of them flipping outcome."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    agent_dir = tmp_path / ".pytest-agent"
    _write_run(
        agent_dir,
        1,
        [
            _record("tests/test_a.py::test_flaky", "passed"),
            _record("tests/test_a.py::test_steady", "passed"),
        ],
    )
    _write_run(
        agent_dir,
        2,
        [
            _record("tests/test_a.py::test_flaky", "failed", **_crash("AssertionError: assert 1 == 2")),
            _record("tests/test_a.py::test_steady", "passed"),
        ],
    )
    _write_run(
        agent_dir,
        3,
        [
            _record("tests/test_a.py::test_flaky", "failed", **_crash("AssertionError: assert 1 == 2")),
            _record("tests/test_a.py::test_steady", "passed"),
            _record("tests/test_new.py::test_added", "failed", **_crash("ValueError: brand new")),
        ],
    )
    return tmp_path


def test_history_answers_whether_a_failure_is_new_or_was_already_there(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["history", "test_flaky"]) == 0

    out = capsys.readouterr().out
    # The count is the whole point: "failed in 2 of the 3 runs" is the
    # evidence a claim of "pre-existing" needs, and the per-run list below it
    # says *which* runs, so the next question has somewhere to go.
    assert "failed in 2 of the 3 runs" in out
    assert "runs-0003  failed" in out
    assert "runs-0001  passed" in out
    assert "AssertionError: assert 1 == 2" in out


def test_history_says_out_loud_that_pruned_runs_are_not_in_the_count(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["history", "test_flaky"]) == 0

    out = capsys.readouterr().out
    # --agent-keep-runs prunes runs-* while history.jsonl keeps its entries
    # forever, so the denominator is silently truncated. An agent about to
    # write "this has always failed" needs to see the limit of what was read.
    assert "3 runs on disk (runs-0001..runs-0003)" in out
    assert "not the full history" in out


def test_history_reports_a_test_that_only_some_runs_ran(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["history", "test_added"]) == 0

    out = capsys.readouterr().out
    # Counted against the runs that ran it, not all three: a test added
    # yesterday has not been "passing 0 of 3 runs".
    assert "failed in 1 of the 1 runs that ran it" in out
    assert "runs-0001  (not in this run)" in out


def test_history_of_an_unknown_test_names_what_was_searched(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["history", "test_nonexistent"]) == 1
    assert "no test matching 'test_nonexistent' in 3 runs on disk" in capsys.readouterr().err


def test_history_limit_looks_at_the_newest_runs_only(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["history", "test_flaky", "--limit", "2"]) == 0

    out = capsys.readouterr().out
    assert "failed in 2 of the 2 runs" in out
    assert "runs-0001" not in out


def test_compare_defaults_to_the_two_newest_runs_and_names_what_changed(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["compare"]) == 0

    out = capsys.readouterr().out
    assert "runs-0002 -> runs-0003" in out
    # test_flaky failed in both, so it is *not* news; the new test is only in
    # runs-0003, so it can't be "newly failing" either -- it is counted apart.
    assert "0 newly failing, 0 newly passing, 1 still failing" in out
    assert "only in runs-0003: 1 tests" in out


def test_compare_of_two_named_runs_separates_newly_failing_from_newly_passing(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["compare", "1", "2"]) == 0

    out = capsys.readouterr().out
    assert "1 newly failing, 0 newly passing, 0 still failing" in out
    assert "newly failing:" in out
    assert "tests/test_a.py::test_flaky" in out
    assert "AssertionError: assert 1 == 2" in out
    # A test that passed in both is not mentioned at all -- the point of the
    # command is the delta, and 900 unchanged passes would bury it.
    assert "test_steady" not in out


def test_compare_the_other_way_round_reports_the_fix(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["compare", "2", "1"]) == 0

    out = capsys.readouterr().out
    assert "0 newly failing, 1 newly passing" in out
    assert "newly passing:" in out


def test_compare_with_one_run_number_says_what_it_wanted(
    three_runs: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del three_runs
    assert run(["compare", "2"]) == 1
    assert "compare takes two runs, or none at all" in capsys.readouterr().err


def test_compare_needs_two_runs_to_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    _write_run(tmp_path / ".pytest-agent", 1, [_record("tests/test_a.py::test_ok", "passed")])

    assert run(["compare"]) == 1
    assert "only 1 run(s)" in capsys.readouterr().err


def test_compare_reports_no_change_rather_than_printing_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)
    agent_dir = tmp_path / ".pytest-agent"
    for number in (1, 2):
        _write_run(agent_dir, number, [_record("tests/test_a.py::test_ok", "passed")])

    assert run(["compare"]) == 0
    # Silence would read as "the command didn't work", which costs a turn to
    # rule out; "nothing changed" is the answer to the question asked.
    assert "no outcome changed between these runs" in capsys.readouterr().out


def test_a_query_against_a_run_that_is_still_going_says_its_records_are_incomplete(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # index.jsonl grows as tests finish, so a run in progress is
    # indistinguishable from a finished one with fewer tests. "0 failures"
    # from a run that has barely started is the worst answer this tool could
    # give, and the lock that protects a live run from pruning makes it
    # detectable.
    create_run_lock(project / ".pytest-agent" / "runs-0001")

    assert run(["last-failures"]) == 0

    captured = capsys.readouterr()
    assert "runs-0001 is still running -- its records are incomplete" in captured.err
    # On stderr, not stdout: the answer itself stays pipeable.
    assert "still running" not in captured.out


def test_querying_a_directory_with_no_runs_explains_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTEST_AGENT_DIR", raising=False)

    assert run(["digest"]) == 1

    assert "run `pytest --agent` first" in capsys.readouterr().err


def test_a_run_can_be_queried_by_the_label_it_was_started_with(
    project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The point of a label: an agent that started a long suite in the
    # background doesn't know, and shouldn't have to work out, how many
    # focused runs happened while it waited -- so it can't name the run by
    # number, only by the name it chose up front.
    write_run_meta(project / ".pytest-agent" / "runs-0001", {"run": 1, "label": "full-suite"})

    assert run(["last-failures", "--run", "full-suite"]) == 0

    out = capsys.readouterr().out
    # The label is echoed back: "did it read the run I meant?" is worth
    # answering without being asked, once runs can be named two ways.
    assert "runs-0001 [full-suite]" in out
    assert "3 failed/errored of 4 recorded" in out


def test_a_labeled_run_can_still_be_queried_by_number(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run_meta(project / ".pytest-agent" / "runs-0001", {"run": 1, "label": "full-suite"})

    assert run(["last-failures", "--run", "1"]) == 0

    assert "runs-0001 [full-suite]" in capsys.readouterr().out


def test_an_unknown_label_says_which_labels_do_exist(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A typo'd label and a pruned one look identical from here, and the
    # difference matters: one is fixed by re-reading, the other by re-running.
    # Listing what is on disk settles which without another command.
    write_run_meta(project / ".pytest-agent" / "runs-0001", {"run": 1, "label": "full-suite"})

    assert run(["last-failures", "--run", "ful-suite"]) == 1

    err = capsys.readouterr().err
    assert "no run labeled 'ful-suite'" in err
    assert "'full-suite'" in err


def test_asking_for_a_label_when_nothing_is_labeled_says_so(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del project
    assert run(["last-failures", "--run", "nightly"]) == 1

    err = capsys.readouterr().err
    assert "no runs on disk are labeled" in err


def test_the_newest_run_wins_a_label_two_runs_share(three_runs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Nothing enforces uniqueness -- re-running one command with the same
    # label is a natural thing to do -- so the rule is stated rather than left
    # to whatever order the directory happens to be read in.
    agent_dir = three_runs / ".pytest-agent"
    write_run_meta(agent_dir / "runs-0001", {"run": 1, "label": "nightly"})
    write_run_meta(agent_dir / "runs-0003", {"run": 3, "label": "nightly"})

    assert run(["last-failures", "--run", "nightly"]) == 0

    captured = capsys.readouterr()
    assert "runs-0003 [nightly]" in captured.out
    # Said out loud, on stderr: the answer is right, but "which one did it
    # pick?" is a question the caller now has, and silence answers it wrong.
    assert "2 runs are labeled 'nightly'" in captured.err
    assert "runs-0003" in captured.err


def test_compare_accepts_labels_on_either_side(three_runs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent_dir = three_runs / ".pytest-agent"
    write_run_meta(agent_dir / "runs-0001", {"run": 1, "label": "before"})
    write_run_meta(agent_dir / "runs-0002", {"run": 2, "label": "after"})

    assert run(["compare", "before", "after"]) == 0

    out = capsys.readouterr().out
    assert "runs-0001 -> runs-0002" in out
    assert "1 newly failing" in out
