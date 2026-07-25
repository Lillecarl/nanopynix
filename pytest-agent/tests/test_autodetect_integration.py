# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import conftest
import pytest

# The PYTHONPATH wiring and harness-env-var cleanup these subprocess runs
# depend on live in conftest._clean_agent_env, which is autouse for every
# test in this package -- and matters most here, since this repo's own dev
# environment really does set CLAUDECODE.


def _agent_dir_was_written(pytester: pytest.Pytester) -> bool:
    return (pytester.path / ".pytest-agent" / "history.jsonl").exists()


def test_agent_mode_stays_off_with_no_harness_env_var_present(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "-q")
    assert result.ret == pytest.ExitCode.OK
    assert not _agent_dir_was_written(pytester)


def test_agent_mode_turns_on_by_itself_when_a_harness_env_var_is_set(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "-q")
    assert result.ret == pytest.ExitCode.OK
    assert _agent_dir_was_written(pytester)


def test_no_autodetect_env_var_disables_the_automatic_activation(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PYTEST_AGENT_NO_AUTODETECT", "1")
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "-q")
    assert result.ret == pytest.ExitCode.OK
    assert not _agent_dir_was_written(pytester)


def test_explicit_agent_flag_still_works_alongside_no_autodetect(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_AGENT_NO_AUTODETECT", "1")
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert result.ret == pytest.ExitCode.OK
    assert _agent_dir_was_written(pytester)
