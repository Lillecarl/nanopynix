# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import conftest
import pytest
from pytest_agent._runtime import DEFAULT_MAX_TERMINAL_SUMMARY_LINES, coverage_total

# The PYTHONPATH wiring and harness-env-var cleanup these subprocess runs
# depend on live in conftest._clean_agent_env, which is autouse for every
# test in this package.


def test_agent_mode_writes_per_test_detail_and_exits_nonzero_on_failure(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_sample="""
        def test_ok():
            print("hello from ok")
            assert True

        def test_fails():
            assert 1 == 2

        def test_skips():
            import pytest
            pytest.skip("nope")
        """,
    )

    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(),
        "--agent",
        "--agent-dir=.pytest-agent",
        "-q",
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    agent_dir = pytester.path / ".pytest-agent"
    # A fresh --agent-dir always starts numbering at 1.
    run_dir = agent_dir / "runs-0001"
    index_lines = (run_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in index_lines]
    outcomes = {record["nodeid"].split("::")[-1]: record["outcome"] for record in records}
    assert outcomes["test_ok"] == "passed"
    assert outcomes["test_fails"] == "failed"
    assert outcomes["test_skips"] == "skipped"

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["passed"] == 1
    assert summary["counts"]["failed"] == 1
    assert summary["counts"]["skipped"] == 1

    fail_log = next(run_dir.rglob("test_fails.log"))
    fail_text = fail_log.read_text(encoding="utf-8")
    # Regression guard: silencing the builtin terminal reporter must not break
    # pytest's own assertion-rewrite comparison output. An earlier
    # implementation fully unregistered the "terminalreporter" plugin, which
    # made Config.get_terminal_writer()'s internal assert fire while
    # rendering *this exact* comparison, replacing the real failure below
    # with an unrelated internal AssertionError.
    assert "assert 1 == 2" in fail_text
    assert "get_terminal_writer" not in fail_text

    ok_log = next(run_dir.rglob("test_ok.log"))
    assert "hello from ok" in ok_log.read_text(encoding="utf-8")


def test_repeated_runs_accumulate_history_and_prune_old_run_dirs(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    agent_dir = pytester.path / ".pytest-agent"

    for _ in range(3):
        result = pytester.runpytest_subprocess(
            *conftest.agent_plugin_cli_args(), "--agent", "--agent-keep-runs=2", "-q"
        )
        assert result.ret == pytest.ExitCode.OK

    run_dirs = sorted(p.name for p in agent_dir.iterdir() if p.name.startswith("runs-"))
    assert run_dirs == ["runs-0002", "runs-0003"]

    # A finished run releases the lock it claimed its directory with, so later
    # runs can prune it -- the assertion above is only true because they do.
    assert [name for name in run_dirs if (agent_dir / name / ".lock").exists()] == []

    history_lines = (agent_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
    history_records = [json.loads(line) for line in history_lines]
    assert [record["run"] for record in history_records] == [1, 2, 3]
    for record in history_records:
        assert record["hostname"]
        assert record["started_at"]
        assert record["counts"]["passed"] == 1

    # history.jsonl's last line is how to find the most recent run without a
    # "latest" symlink -- no separate mutable pointer to keep race-free.
    assert history_records[-1]["run_dir"] == "runs-0003"


def test_a_finishing_run_does_not_prune_a_run_another_process_is_still_writing(
    pytester: pytest.Pytester,
) -> None:
    # Two real pytest processes against one --agent-dir, which is the only way
    # to exercise this: run numbers are handed out at session start, so a run
    # that starts later holds a higher number and can finish -- and prune --
    # while an earlier, lower-numbered run is still writing. With
    # --agent-keep-runs=1 the live run sits outside the newest N, and pruning
    # by number alone would delete the directory out from under it.
    # A test that finishes before the hang, so the live run has records on
    # disk to lose -- a surviving-but-emptied directory would otherwise pass.
    pytester.makepyfile(
        test_hang="import time\n\n\ndef test_first():\n    assert True\n\n\ndef test_hangs():\n    time.sleep(300)\n",
    )
    pytester.makepyfile(test_quick="def test_ok():\n    assert True\n")
    agent_dir = pytester.path / ".pytest-agent"
    first_run = agent_dir / "runs-0001"
    index_path = first_run / "index.jsonl"

    with conftest.running_pytest(pytester.path, "test_hang.py", "--agent-heartbeat", "0.2") as hanging:
        conftest.wait_until(
            lambda: index_path.is_file() and "test_first" in index_path.read_text(encoding="utf-8"),
            "the hanging run to record its first test",
        )

        finished = conftest.run_cli(["test_quick.py", "--agent-keep-runs=1"], cwd=pytester.path)
        assert finished.returncode == 0, finished.stderr

        assert (agent_dir / "runs-0002").is_dir()
        assert first_run.is_dir(), "the live run's directory was pruned out from under it"
        assert (first_run / ".lock").is_file()
        # The directory surviving is not the point; what it holds is.
        assert "test_first" in index_path.read_text(encoding="utf-8")

        hanging.send_signal(signal.SIGTERM)
        hanging.communicate(timeout=conftest.WAIT_TIMEOUT)

    # The other half: the lock is a loan, not an exemption. Once that run
    # ends, its directory rejoins the rotation like any other.
    assert not (first_run / ".lock").exists()
    reaped = conftest.run_cli(["test_quick.py", "--agent-keep-runs=1"], cwd=pytester.path)
    assert reaped.returncode == 0, reaped.stderr
    assert not first_run.exists()


def test_a_real_failure_records_structured_crash_data_and_first_party_frames(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        helper="""
        def explode():
            raise FileNotFoundError("/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-swagger.json")
        """,
    )
    pytester.makepyfile(
        test_sample="""
        import helper

        def test_boom():
            helper.explode()
        """,
    )

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    index_path = pytester.path / ".pytest-agent" / "runs-0001" / "index.jsonl"
    records = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    record = next(record for record in records if record["outcome"] == "failed")

    assert record["crash"]["exc_type"] == "FileNotFoundError"
    assert "swagger.json" in record["crash"]["message"]
    assert record["crash"]["path"] == "helper.py"
    # Both the test and the helper it called are the code under test, and
    # pytest's own frames (site-packages) must not be mistaken for them.
    frame_paths = [frame["path"] for frame in record["frames"] if frame["first_party"]]
    assert frame_paths == ["test_sample.py", "helper.py"]


def test_a_passing_run_records_no_crash_fields(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")

    index_path = pytester.path / ".pytest-agent" / "runs-0001" / "index.jsonl"
    record = json.loads(index_path.read_text(encoding="utf-8").splitlines()[0])
    assert "crash" not in record
    assert "frames" not in record


def test_the_final_summary_names_each_failure_s_log_file(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_bad():\n    assert False\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")

    # The resolved path and nothing else: it can be read straight back
    # without being reassembled from the run number and the test file's
    # path, and it already spells out the nodeid.
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    printed = "\n".join(result.outlines)
    assert "  .pytest-agent/runs-0001/test_sample.py/test_bad.log" in printed
    assert "test_sample.py::test_bad" not in printed
    assert (pytester.path / ".pytest-agent/runs-0001/test_sample.py/test_bad.log").is_file()


def test_the_final_summary_adds_the_nodeid_when_the_path_lost_it(pytester: pytest.Pytester) -> None:
    # A `/` in a parametrized id becomes `_` in the file name, so here the
    # path alone can't be read back as a nodeid and both are printed.
    pytester.makepyfile(
        test_sample="import pytest\n\n@pytest.mark.parametrize('x', ['a/b'])\ndef test_p(x):\n    assert False\n",
    )

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    printed = "\n".join(result.outlines)
    # Shell-quoted, because the brackets would otherwise glob (fish refuses
    # such a path outright). The name carries a hash of the id it came from,
    # so it cannot also stand for a differently-spelled test that sanitizes
    # to the same thing -- see the collision test below.
    logs = list((pytester.path / ".pytest-agent/runs-0001/test_sample.py").glob("test_p[[]a_b[]]~*.log"))
    assert len(logs) == 1, f"expected one disambiguated log, got {logs}"
    assert f"'.pytest-agent/runs-0001/test_sample.py/{logs[0].name}'  (test_sample.py::test_p[a/b])" in printed


def test_two_tests_whose_names_sanitize_alike_keep_separate_logs(pytester: pytest.Pytester) -> None:
    # test_p[a/b] and test_p[a_b] are two different tests that both sanitize
    # to "test_p[a_b]". The second to finish silently overwrote the first's
    # log, so `show` on the failing one printed the passing one's detail --
    # a wrong answer, which is worse than no answer.
    pytester.makepyfile(
        test_sample="""
        import pytest


        @pytest.mark.parametrize("x", ["a/b", "a_b"])
        def test_p(x):
            assert x != "a/b", "only the slash one fails"
        """,
    )

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    run_dir = pytester.path / ".pytest-agent" / "runs-0001"
    logs = sorted(path.name for path in (run_dir / "test_sample.py").glob("*.log"))
    assert len(logs) == 2, f"one test's detail overwrote the other's: {logs}"

    records = [json.loads(line) for line in (run_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    by_nodeid = {record["nodeid"]: record for record in records}
    # Each record points at its own file, and that file describes that test.
    for nodeid, record in by_nodeid.items():
        text = (run_dir / record["log_file"]).read_text(encoding="utf-8")
        assert f"nodeid: {nodeid}" in text
    assert by_nodeid["test_sample.py::test_p[a/b]"]["outcome"] == "failed"
    assert by_nodeid["test_sample.py::test_p[a_b]"]["outcome"] == "passed"


def test_the_cli_dispatches_query_subcommands_instead_of_running_pytest(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_sample="def test_ok():\n    assert True\n\ndef test_bad():\n    raise ValueError('nope')\n"
    )
    pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")

    digest = conftest.run_cli(["digest"], cwd=pytester.path)

    assert digest.returncode == 0, digest.stderr
    assert "1x  ValueError: nope" in digest.stdout
    # No second run directory: a query must not start a pytest session.
    assert not (pytester.path / ".pytest-agent" / "runs-0002").exists()


def test_cli_wrapper_forces_agent_mode_on_with_no_flags(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = conftest.run_cli([], cwd=pytester.path)

    assert result.returncode == 0, result.stderr
    assert (pytester.path / ".pytest-agent" / "runs-0001" / "index.jsonl").exists()


def test_pipe_guard_blocks_a_run_piped_into_grep(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    grep = subprocess.Popen(["grep", "x"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    if grep.stdin is None:
        raise AssertionError("expected grep's stdin to be a pipe")

    pytest_proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *conftest.agent_plugin_cli_args()],
        cwd=pytester.path,
        stdout=grep.stdin,
        stderr=subprocess.PIPE,
    )
    grep.stdin.close()
    _stdout, stderr = pytest_proc.communicate(timeout=10)
    grep.wait(timeout=10)

    assert pytest_proc.returncode == 2
    assert b"pytest-agent: refusing to run" in stderr


def test_collect_only_may_be_piped_because_there_is_no_detail_to_lose(pytester: pytest.Pytester) -> None:
    # `pytest --collect-only -q | tail -1` is how you ask "how many tests
    # does this select". There are no failures to hide, and the interesting
    # line is deliberately last, so the guard's rationale does not apply.
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    tail = subprocess.Popen(["tail", "-n", "1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    if tail.stdin is None:
        raise AssertionError("expected tail's stdin to be a pipe")

    pytest_proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *conftest.agent_plugin_cli_args(), "--collect-only", "-q"],
        cwd=pytester.path,
        stdout=tail.stdin,
        stderr=subprocess.PIPE,
    )
    tail.stdin.close()
    _stdout, stderr = pytest_proc.communicate(timeout=10)
    tail_out, _ = tail.communicate(timeout=10)

    assert pytest_proc.returncode == 0
    assert b"pytest-agent: refusing to run" not in stderr
    assert b"1 test collected" in tail_out


def test_agent_mode_leaves_a_listing_only_run_completely_alone(pytester: pytest.Pytester) -> None:
    # Agent mode silences the terminal reporter and claims a run directory.
    # Both are wrong for --collect-only: the listing *is* the answer, so
    # silencing it leaves the caller with nothing, and there is no per-test
    # detail for a run directory to hold.
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "--collect-only", "-q")

    assert result.ret == pytest.ExitCode.OK
    assert any("1 test collected" in line for line in result.outlines)
    assert not (pytester.path / ".pytest-agent").exists()


def test_agent_allow_pipe_bypasses_the_guard(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    grep = subprocess.Popen(["grep", "-q", "."], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    if grep.stdin is None:
        raise AssertionError("expected grep's stdin to be a pipe")

    pytest_proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *conftest.agent_plugin_cli_args(), "--agent-allow-pipe", "-q"],
        cwd=pytester.path,
        stdout=grep.stdin,
        stderr=subprocess.PIPE,
    )
    grep.stdin.close()
    _stdout, stderr = pytest_proc.communicate(timeout=10)
    grep.wait(timeout=10)

    assert pytest_proc.returncode == 0
    assert b"pytest-agent: refusing to run" not in stderr


def test_a_labeled_background_run_is_still_there_to_be_asked_about_afterwards(pytester: pytest.Pytester) -> None:
    # The whole point of labels, end to end: start the long suite in the
    # background, do focused runs while it goes, then come back and ask it
    # what failed -- without knowing how many runs happened in between, which
    # is the one thing an agent can't know in advance and so can't encode in a
    # run number.
    pytester.makepyfile(
        test_slow="""
        import time


        def test_fails_early():
            raise AssertionError("the failure the long run was for")


        def test_takes_forever():
            time.sleep(300)
        """,
    )
    pytester.makepyfile(test_quick="def test_ok():\n    assert True\n")
    agent_dir = pytester.path / ".pytest-agent"
    labeled = agent_dir / "runs-0001"
    index_path = labeled / "index.jsonl"

    with conftest.running_pytest(
        pytester.path,
        "test_slow.py",
        "--agent-label",
        "full-suite",
        "--agent-heartbeat",
        "0.2",
    ) as background:
        conftest.wait_until(
            lambda: index_path.is_file() and "test_fails_early" in index_path.read_text(encoding="utf-8"),
            "the labeled run to record its failure",
        )
        # Enough focused runs to push the labeled one well out of the newest
        # N, which is what would have deleted it before anyone asked.
        for _ in range(3):
            focused = conftest.run_cli(["test_quick.py", "--agent-keep-runs=1"], cwd=pytester.path)
            assert focused.returncode == 0, focused.stderr

        background.send_signal(signal.SIGTERM)
        background.communicate(timeout=conftest.WAIT_TIMEOUT)

    # One more *after* the labeled run released its lock, so what keeps it
    # alive from here is the labeled retention budget and nothing else.
    assert not (labeled / ".lock").exists()
    after = conftest.run_cli(["test_quick.py", "--agent-keep-runs=1"], cwd=pytester.path)
    assert after.returncode == 0, after.stderr
    assert labeled.is_dir(), "the labeled run was pruned before anyone could ask about it"

    asked = conftest.run_cli(["last-failures", "--run", "full-suite"], cwd=pytester.path)
    assert asked.returncode == 0, asked.stderr
    assert "runs-0001 [full-suite]" in asked.stdout
    assert "test_fails_early" in asked.stdout
    # And the focused runs it outlived really are gone -- otherwise this
    # would pass with pruning switched off entirely.
    assert not (agent_dir / "runs-0002").exists()


def test_another_plugins_end_of_run_report_is_not_swallowed(pytester: pytest.Pytester) -> None:
    # Agent mode redirects the terminal reporter away from the terminal, which
    # for a long time meant os.devnull -- so every plugin reporting through it
    # produced nothing at all. Asking for a report and getting neither the
    # report nor an error is the worst way to lose output.
    pytester.makeconftest(
        """
        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line("PLUGIN-REPORT: 42% of something")
        """,
    )
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    assert "also reported by other plugins" in out
    assert "PLUGIN-REPORT: 42% of something" in out
    # Printed raw, without the [pytest-agent] prefix the rest of the output
    # carries: these are somebody else's aligned tables.
    assert "[pytest-agent] PLUGIN-REPORT" not in out

    terminal_log = pytester.path / ".pytest-agent" / "runs-0001" / "terminal.txt"
    assert "PLUGIN-REPORT: 42% of something" in terminal_log.read_text(encoding="utf-8")


def test_durations_asked_for_on_the_command_line_actually_appear(pytester: pytest.Pytester) -> None:
    # The builtin case, and the one an agent reaches for when a suite got
    # slow: --durations is a plain pytest_terminal_summary in _pytest.runner,
    # so it went the same way as every third-party report.
    pytester.makepyfile(
        test_sample="""
        import time


        def test_slowish():
            time.sleep(0.05)
        """,
    )

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "--durations=3")
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    assert "slowest 3 durations" in out
    assert "test_slowish" in out


# Row counts for the report fixtures below, either side of the default bound.
# Derived from it rather than written as literals: a changed default would
# otherwise turn the tests using them vacuous rather than failing.
_SHORT_REPORT_ROWS = DEFAULT_MAX_TERMINAL_SUMMARY_LINES // 8
_LONG_REPORT_ROWS = DEFAULT_MAX_TERMINAL_SUMMARY_LINES * 2


def _report_conftest(rows: int, *, total_row: str = "REPORT-TOTAL") -> str:
    return f"""
        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            terminalreporter.write_line("REPORT-HEADER")
            for n in range({rows}):
                terminalreporter.write_line(f"row {{n}}")
            terminalreporter.write_line({total_row!r})
        """


def test_a_short_report_is_printed_where_it_can_be_read(pytester: pytest.Pytester) -> None:
    # Under the bound nothing is held back. `--durations`, a junit path, a
    # small table: reading them costs a glance, and sending somebody to a file
    # for five lines would be worse than printing them.
    pytester.makeconftest(_report_conftest(_SHORT_REPORT_ROWS))
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    assert "not shown" not in out
    assert out.count("\nrow ") == _SHORT_REPORT_ROWS


def test_a_report_past_the_bound_is_pointed_at_rather_than_shown_in_pieces(pytester: pytest.Pytester) -> None:
    # The regression. `pytest --cov --cov-report=term-missing` on the suite
    # this package was written for produces an 88-line table, and the old
    # behaviour printed its first 20 and last 15 lines -- column headings,
    # TOTAL, and none of the per-file rows, which is what a coverage table
    # *is*. A fragment of a table is worse than a pointer to the whole one,
    # because it reads as the whole one.
    pytester.makeconftest(_report_conftest(_LONG_REPORT_ROWS))
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    # Its own first line survives, so the pointer says *which* report is
    # waiting rather than only how long it was.
    assert "REPORT-HEADER" in out
    assert out.count("\nrow ") == 0, "no fragment of the table, in either direction"
    assert "REPORT-TOTAL" not in out
    # The path is repeated at the point of the cut, resolved: this is where
    # somebody stops when the content they came for is missing.
    assert f"{_LONG_REPORT_ROWS + 1} more report lines not shown; full text in" in out
    assert f"{Path('.pytest-agent') / 'runs-0001' / 'reports.txt'}" in out
    # Named where it is needed. The bound is a preference, and somebody who
    # wants the table inline should not have to go and find out how.
    assert "--agent-max-summary-lines=0" in out

    reports = (pytester.path / ".pytest-agent" / "runs-0001" / "reports.txt").read_text(encoding="utf-8")
    assert f"row {_LONG_REPORT_ROWS // 2}" in reports, "everything held back is on disk"
    assert "REPORT-TOTAL" in reports


def test_the_coverage_line_survives_the_report_being_held_back(pytester: pytest.Pytester) -> None:
    # The case the headline number matters most in, and the one where reading
    # it off the table is not available at all. The percentage is taken from
    # the whole report rather than from the part that got printed.
    pytester.makeconftest(_report_conftest(_LONG_REPORT_ROWS, total_row="TOTAL      184      6    97%"))
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    assert "not shown" in out, "the report has to be over budget for this to test anything"
    assert "TOTAL      184" not in out, "and its TOTAL row genuinely did not reach the terminal"
    assert "[pytest-agent] coverage: 97%" in out


def test_the_reports_file_holds_the_report_and_only_the_report(pytester: pytest.Pytester) -> None:
    # The pointer has to lead somewhere that needs no searching. terminal.txt
    # has this content too, but wrapped in the whole pytest transcript --
    # session header, tracebacks, short summary -- and an agent that follows a
    # pointer into a mixed file can come back with the wrong half of it.
    pytester.makeconftest(_report_conftest(3))
    pytester.makepyfile(test_sample="def test_fails():\n    assert 1 == 2\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    run_dir = pytester.path / ".pytest-agent" / "runs-0001"
    reports = (run_dir / "reports.txt").read_text(encoding="utf-8")
    assert reports.splitlines() == ["REPORT-HEADER", "row 0", "row 1", "row 2", "REPORT-TOTAL"]
    # The transcript it was lifted out of still has everything, including the
    # failure this run also produced -- which reports.txt must not.
    terminal = (run_dir / "terminal.txt").read_text(encoding="utf-8")
    assert "REPORT-TOTAL" in terminal
    assert "test session starts" in terminal
    assert "test session starts" not in reports
    assert "test_fails" not in reports

    out = "\n".join(result.outlines)
    assert f"also reported by other plugins ({Path('.pytest-agent') / 'runs-0001' / 'reports.txt'})" in out


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        pytest.param(["TOTAL                        184      6    97%"], "97%", id="total-row"),
        pytest.param(["TOTAL      184   6   96.85%   88-93, 101"], "96.85%", id="total-row-with-missing"),
        pytest.param(["Required test coverage of 80% reached. Total coverage: 96.85%"], "96.85%", id="fail-under"),
        pytest.param(["Name    Stmts", "mypkg/a.py    12    100%"], None, id="no-total-row"),
        pytest.param(["TOTAL DURATION 4.1s"], None, id="someone-elses-total"),
        pytest.param([], None, id="empty"),
    ],
)
def test_the_coverage_total_is_read_out_of_the_report_text(report: list[str], expected: str | None) -> None:
    # Read from the text rather than from pytest-cov's plugin object, so this
    # is where the shapes it has to survive are written down. The two negatives
    # matter as much as the positives: a plugin that prints its own TOTAL of
    # something else must not be reported as coverage.
    assert coverage_total(report) == expected


def test_a_real_cov_run_puts_the_percentage_on_its_own_line(pytester: pytest.Pytester) -> None:
    # End to end against the actual plugin, because the unit test above pins
    # this package's reading of pytest-cov's format and nothing pins that the
    # format is still that.
    pytest.importorskip("pytest_cov")
    pytester.makepyfile(
        mypkg="""
        def covered():
            return 1


        def uncovered():
            return 2
        """,
    )
    pytester.makepyfile(test_sample="import mypkg\n\n\ndef test_ok():\n    assert mypkg.covered() == 1\n")

    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(),
        "--agent",
        "--cov=mypkg",
        "--cov-report=term-missing",
    )
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    total = re.search(r"^\[pytest-agent\] coverage: (\d+(?:\.\d+)?)%$", out, re.MULTILINE)
    assert total is not None, out
    # The package has one uncovered return, so this is a real measurement and
    # not a table of zeroes that would match any regex.
    assert 0 < float(total.group(1)) < 100

    reports = (pytester.path / ".pytest-agent" / "runs-0001" / "reports.txt").read_text(encoding="utf-8")
    assert "mypkg.py" in reports
    assert f"{total.group(1)}%" in reports


@pytest.mark.parametrize(
    ("budget", "holds_back"),
    [("500", False), ("0", False), ("3", True)],
    ids=["raised", "disabled", "lowered"],
)
def test_the_report_bound_is_tunable(pytester: pytest.Pytester, budget: str, holds_back: bool) -> None:
    # Where the line falls is project-dependent -- a monorepo's coverage table
    # is a different animal from a five-file package's -- so it is a knob
    # rather than a number this package picks for everybody. 0 means no bound,
    # matching --agent-heartbeat's reading of 0.
    pytester.makeconftest(_report_conftest(_LONG_REPORT_ROWS))
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(),
        "--agent",
        f"--agent-max-summary-lines={budget}",
    )
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    assert ("not shown" in out) is holds_back
    assert (out.count("\nrow ") == _LONG_REPORT_ROWS) is not holds_back


def test_a_run_with_nothing_extra_to_report_stays_as_quiet_as_before(pytester: pytest.Pytester) -> None:
    # The whole point of agent mode is a terminal that stays short. Capturing
    # the reporter's output must not turn into printing it.
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.OK

    out = "\n".join(result.outlines)
    assert "also reported by other plugins" not in out
    assert "test session starts" not in out


def test_pytests_own_output_is_kept_on_disk_rather_than_destroyed(pytester: pytest.Pytester) -> None:
    # What plain pytest would have printed is a real artifact -- the thing to
    # read when a failure is about pytest's own reporting rather than about a
    # test. Redirected, not discarded.
    pytester.makepyfile(test_sample="def test_fails():\n    assert 1 == 2\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    text = (pytester.path / ".pytest-agent" / "runs-0001" / "terminal.txt").read_text(encoding="utf-8")
    assert "test session starts" in text
    assert "assert 1 == 2" in text
    assert "1 failed" in text


def test_a_test_with_an_enormous_parametrized_id_does_not_abort_the_run(pytester: pytest.Pytester) -> None:
    # This used to end the session with an INTERNALERROR and no results at
    # all -- OSError: File name too long, raised while writing the test's log
    # -- and only under --agent. A suite that plain pytest runs fine must not
    # be un-runnable because of the plugin watching it.
    pytester.makepyfile(
        test_long="""
        import pytest


        @pytest.mark.parametrize("payload", ["x" * 400])
        def test_with_a_long_id(payload):
            assert len(payload) == 400


        def test_after_the_long_one():
            assert True
        """,
    )

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    assert result.ret == pytest.ExitCode.OK

    index_path = pytester.path / ".pytest-agent" / "runs-0001" / "index.jsonl"
    records = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2, "the test after the long one must still have run"
    assert all(record["outcome"] == "passed" for record in records)
    assert all("capture_error" not in record for record in records)

    long_record = next(record for record in records if "long_id" in record["nodeid"])
    log_path = pytester.path / ".pytest-agent" / "runs-0001" / long_record["log_file"]
    assert log_path.is_file(), "the shortened name must be a name the filesystem accepts"
    # Every component fits, including the longest suffix the run may add.
    assert all(len(part.encode("utf-8")) <= 255 for part in log_path.parts)
    assert long_record["nodeid"] in log_path.read_text(encoding="utf-8")


def test_an_unwritable_agent_dir_turns_agent_mode_off_instead_of_killing_the_run(
    pytester: pytest.Pytester,
) -> None:
    # A read-only checkout, a CI workspace mounted read-only, a .pytest-agent
    # left root-owned by a container. This used to be an INTERNALERROR in
    # pytest_configure: no tests ran at all, because of the plugin watching
    # them. Agent mode is a way of watching a run, so it must never be the
    # reason one cannot happen.
    agent_dir = pytester.path / ".pytest-agent"
    agent_dir.mkdir()
    agent_dir.chmod(0o500)
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    try:
        result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent")
    finally:
        agent_dir.chmod(0o700)

    assert result.ret == pytest.ExitCode.OK
    # Plain pytest output, because agent mode never silenced the reporter.
    result.stdout.fnmatch_lines(["*1 passed*"])
    # And it said so, rather than leaving the caller to notice the archive
    # stopped growing -- a later `last-failures` would answer from a stale run.
    result.stderr.fnmatch_lines(["*agent mode is OFF for this run*", "*somewhere writable*"])


def test_the_nodeid_printed_for_a_long_test_is_one_show_can_resolve(
    pytester: pytest.Pytester,
) -> None:
    # The failure line appends the nodeid exactly when the log path lost it,
    # which for a name over MAX_NAME_BYTES makes that copy the only addressable
    # form left. Shortening it for width made the printed id resolve to
    # nothing: `show` takes a nodeid or a unique substring, and an elided
    # middle is neither. So the contract is the round trip, not the width.
    pytester.makepyfile(
        test_sample=("import pytest\n\n@pytest.mark.parametrize('x', ['z' * 400])\ndef test_p(x):\n    assert False\n"),
    )

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    printed = "\n".join(result.outlines)
    match = re.search(r"\((test_sample\.py::test_p\[[^)]*\])\)", printed)
    assert match is not None, f"expected the nodeid alongside the log path in:\n{printed}"
    printed_nodeid = match.group(1)

    shown = conftest.run_cli(["show", printed_nodeid], cwd=pytester.path)
    assert shown.returncode == 0, f"show could not resolve the id it was given:\n{shown.stdout}{shown.stderr}"
    assert "outcome: failed" in shown.stdout
