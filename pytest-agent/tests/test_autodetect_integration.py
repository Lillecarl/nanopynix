# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_agent._harness_detect import HARNESS_ENV_VARS

_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _clean_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    # Same PYTHONPATH wiring as test_integration.py's fixture (subprocesses
    # don't inherit pyproject.toml's `pythonpath` ini option), plus a clean
    # slate for every harness env var: this repo's own dev environment is
    # itself a Claude Code session, so CLAUDECODE and AI_AGENT are genuinely
    # set here and would otherwise make every subprocess in this file
    # auto-activate regardless of what's being tested.
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_SRC), *([existing] if existing else [])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))
    for name in HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTEST_AGENT", raising=False)
    monkeypatch.delenv("PYTEST_AGENT_NO_AUTODETECT", raising=False)


def _agent_dir_was_written(pytester: pytest.Pytester) -> bool:
    return (pytester.path / ".pytest-agent" / "index.jsonl").exists()


def test_agent_mode_stays_off_with_no_harness_env_var_present(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess("-p", "pytest_agent.plugin", "-q")
    assert result.ret == pytest.ExitCode.OK
    assert not _agent_dir_was_written(pytester)


def test_agent_mode_turns_on_by_itself_when_a_harness_env_var_is_set(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess("-p", "pytest_agent.plugin", "-q")
    assert result.ret == pytest.ExitCode.OK
    assert _agent_dir_was_written(pytester)


def test_no_autodetect_env_var_disables_the_automatic_activation(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PYTEST_AGENT_NO_AUTODETECT", "1")
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess("-p", "pytest_agent.plugin", "-q")
    assert result.ret == pytest.ExitCode.OK
    assert not _agent_dir_was_written(pytester)


def test_explicit_agent_flag_still_works_alongside_no_autodetect(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_AGENT_NO_AUTODETECT", "1")
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess("-p", "pytest_agent.plugin", "--agent", "-q")
    assert result.ret == pytest.ExitCode.OK
    assert _agent_dir_was_written(pytester)
