from __future__ import annotations

from typing import TYPE_CHECKING

from pytest_agent.cli import _misspelled_subcommand

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_a_near_miss_of_a_subcommand_is_recognized() -> None:
    assert _misspelled_subcommand(["lastfailures"]) == "last-failures"
    assert _misspelled_subcommand(["digset"]) == "digest"


def test_ordinary_pytest_arguments_are_not_treated_as_typos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()

    assert _misspelled_subcommand([]) is None
    assert _misspelled_subcommand(["-x", "--agent-dir=x"]) is None
    assert _misspelled_subcommand(["tests"]) is None
    assert _misspelled_subcommand(["tests/test_lsp.py::test_hover[in_process]"]) is None
    assert _misspelled_subcommand(["-k", "show"]) is None


def test_a_real_path_named_like_a_subcommand_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `pytest-agent ./show` must reach pytest, not be second-guessed.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "showcase").mkdir()

    assert _misspelled_subcommand(["showcase"]) is None
