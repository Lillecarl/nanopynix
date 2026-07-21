# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import conftest
import pytest
from pytest_agent._harness_detect import HARNESS_ENV_VARS

_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _agent_on_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    # These tests spawn real `pytest` subprocesses to exercise the plugin
    # end-to-end. `pythonpath` in pyproject.toml only affects sys.path for
    # *this* pytest run, not for child processes, so PYTHONPATH is set
    # explicitly for the subprocess to find pytest_agent without installing it.
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_SRC), *([existing] if existing else [])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))
    # This repo's own dev environment is itself a Claude Code session, so
    # cleared here to keep --agent's on/off state in these tests explicit
    # rather than an accident of where they happen to run.
    for name in HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


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
        """
    )

    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(), "--agent", "--agent-dir=.pytest-agent", "-q"
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


def test_cli_wrapper_forces_agent_mode_on_with_no_flags(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv = ['pytest-agent']; from pytest_agent.cli import main; main()"],
        cwd=pytester.path,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode == 0
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
