from __future__ import annotations

import pytest
from pytest_agent._harness_detect import HARNESS_ENV_VARS, detect_agent_harness


@pytest.fixture(autouse=True)
def _clear_harness_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    for name in HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_returns_none_with_no_harness_env_vars_set() -> None:
    assert detect_agent_harness() is None


def test_detects_claudecode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert detect_agent_harness() == "CLAUDECODE"


def test_detects_the_generic_ai_agent_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AGENT", "some-tool")
    assert detect_agent_harness() == "AI_AGENT"


def test_an_empty_string_value_does_not_count_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_AGENT", "")
    assert detect_agent_harness() is None
