"""What ``--file`` offers before the ``#``, and what a ``~`` does to it.

`pynix/completions/tests/test_nix_equivalence.py` compares the ordinary case
against `nix` itself, and that is the gate. This module states the two things
that comparison cannot: a tilde comes back as a tilde, where `nix` answers the
expanded path, and the expansion still reaches the evaluator. Issue #279.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynix._attr_completion import complete_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_a_path_prefix_offers_the_file_and_the_directory(tmp_path: Path) -> None:
    """``Args::completePath`` keeps every glob match, and so does this."""
    (tmp_path / "target.nix").write_text("{ }\n", encoding="utf-8")
    (tmp_path / "target-directory").mkdir()
    (tmp_path / "other.nix").write_text("{ }\n", encoding="utf-8")

    offered = set(complete_file(prefix=str(tmp_path / "targe")))

    assert offered == {str(tmp_path / "target.nix"), str(tmp_path / "target-directory")}


def test_a_prefix_that_names_nothing_offers_nothing(tmp_path: Path) -> None:
    assert list(complete_file(prefix=str(tmp_path / "absent"))) == []


def test_a_tilde_comes_back_as_a_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one place this does not answer what ``nix`` answers.

    `nix` offers the expanded path, and its own bash completion puts that into
    ``COMPREPLY``, which bash does not filter. argcomplete keeps a candidate
    only when ``candidate.startswith(prefix)``, so an expanded answer to a
    tilde prefix costs the caller every candidate.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "project.nix").write_text("{ }\n", encoding="utf-8")

    assert set(complete_file(prefix="~/proj")) == {"~/project.nix"}


def test_a_bare_tilde_offers_the_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The head of the prefix is ``~`` with no separator after it."""
    monkeypatch.setenv("HOME", str(tmp_path))

    assert set(complete_file(prefix="~")) == {"~"}


def test_a_tilde_reaches_the_evaluator_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the ``#`` the path is evaluated, and no directory holds a tilde.

    A shell expands a ``~`` before it runs a command, so a real run never shows
    Nix the tilde. A completion runs before that expansion.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "attrs.nix").write_text('{ alpha = "a"; beta = "b"; }\n', encoding="utf-8")

    offered = set(complete_file(prefix="~/attrs.nix#al"))

    assert offered == {"~/attrs.nix#alpha"}
