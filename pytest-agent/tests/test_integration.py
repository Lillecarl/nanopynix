# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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

    result = pytester.runpytest_subprocess("-p", "pytest_agent.plugin", "--agent", "--agent-dir=.pytest-agent", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    agent_dir = pytester.path / ".pytest-agent"
    index_lines = (agent_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in index_lines]
    outcomes = {record["nodeid"].split("::")[-1]: record["outcome"] for record in records}
    assert outcomes["test_ok"] == "passed"
    assert outcomes["test_fails"] == "failed"
    assert outcomes["test_skips"] == "skipped"

    summary = json.loads((agent_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["passed"] == 1
    assert summary["counts"]["failed"] == 1
    assert summary["counts"]["skipped"] == 1

    fail_log = next((agent_dir / "tests").rglob("test_fails.log"))
    fail_text = fail_log.read_text(encoding="utf-8")
    # Regression guard: silencing the builtin terminal reporter must not break
    # pytest's own assertion-rewrite comparison output. An earlier
    # implementation fully unregistered the "terminalreporter" plugin, which
    # made Config.get_terminal_writer()'s internal assert fire while
    # rendering *this exact* comparison, replacing the real failure below
    # with an unrelated internal AssertionError.
    assert "assert 1 == 2" in fail_text
    assert "get_terminal_writer" not in fail_text

    ok_log = next((agent_dir / "tests").rglob("test_ok.log"))
    assert "hello from ok" in ok_log.read_text(encoding="utf-8")


def test_pipe_guard_blocks_a_run_piped_into_grep(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    grep = subprocess.Popen(["grep", "x"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    if grep.stdin is None:
        raise AssertionError("expected grep's stdin to be a pipe")

    pytest_proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-p", "pytest_agent.plugin"],
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
        [sys.executable, "-m", "pytest", "-p", "pytest_agent.plugin", "--agent-allow-pipe", "-q"],
        cwd=pytester.path,
        stdout=grep.stdin,
        stderr=subprocess.PIPE,
    )
    grep.stdin.close()
    _stdout, stderr = pytest_proc.communicate(timeout=10)
    grep.wait(timeout=10)

    assert pytest_proc.returncode == 0
    assert b"pytest-agent: refusing to run" not in stderr
