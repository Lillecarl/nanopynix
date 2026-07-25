# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import json
import signal
import subprocess
import sys

import conftest
import pytest

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
    # such a path outright).
    assert "'.pytest-agent/runs-0001/test_sample.py/test_p[a_b].log'  (test_sample.py::test_p[a/b])" in printed
    assert (pytester.path / ".pytest-agent/runs-0001/test_sample.py/test_p[a_b].log").is_file()


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
