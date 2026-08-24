"""Tests for the AI-agent-harness detection."""

from __future__ import annotations

import pytest

from libpynix.agent_harness import (
    HARNESS_ENV_VARS,
    detect_agent_harness,
    human_at_terminal,
)


@pytest.fixture(autouse=True)
def _no_harness(  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clear every harness variable, so the test process itself is not one.

    The suite very often runs under an agent harness, which sets these. A test
    that reads the real environment would then pass or fail by accident.
    """
    for name in HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class _Tty:
    """A stdin or stdout double that reports whether it is a terminal."""

    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _streams(monkeypatch: pytest.MonkeyPatch, *, stdin: bool, stdout: bool) -> None:
    monkeypatch.setattr("sys.stdin", _Tty(tty=stdin))
    monkeypatch.setattr("sys.stdout", _Tty(tty=stdout))


def test_no_harness_variable_detects_nothing() -> None:
    assert detect_agent_harness() is None


@pytest.mark.parametrize("name", HARNESS_ENV_VARS)
def test_every_listed_variable_is_detected(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, "1")
    assert detect_agent_harness() == name


def test_an_empty_value_is_not_a_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported but empty variable is how a shell unsets one in practice."""
    monkeypatch.setenv("CLAUDECODE", "")
    assert detect_agent_harness() is None


def test_human_at_terminal_needs_both_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    _streams(monkeypatch, stdin=True, stdout=True)
    assert human_at_terminal() is True

    _streams(monkeypatch, stdin=False, stdout=True)
    assert human_at_terminal() is False

    _streams(monkeypatch, stdin=True, stdout=False)
    assert human_at_terminal() is False


def test_a_harness_is_not_a_human_even_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A harness gives its tool a pseudo-terminal and still reads text."""
    _streams(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert human_at_terminal() is False
