"""Tests for NIX_PATH support (EvalSettings::parseNixPath via nanopynix_expr.parse_nix_path)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import nanopynix_expr


def test_parse_nix_path_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """_parse_nix_path returns [] when NIX_PATH is unset."""
    monkeypatch.delenv("NIX_PATH", raising=False)
    result = nanopynix_expr.parse_nix_path()
    assert result == []


def test_parse_nix_path_simple(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse_nix_path splits colon-separated prefix=path entries."""
    with tempfile.TemporaryDirectory() as d:
        a = str(Path(d) / "a")
        b = str(Path(d) / "b")
        Path(a).mkdir(parents=True)
        Path(b).mkdir(parents=True)
        monkeypatch.setenv("NIX_PATH", f"foo={a}:bar={b}")
        result = nanopynix_expr.parse_nix_path()
    assert len(result) == 2
    assert result[0] == f"foo={a}"
    assert result[1] == f"bar={b}"


def test_parse_nix_path_explicit_value_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse_nix_path can parse a supplied NIX_PATH value outside the worker."""
    monkeypatch.delenv("NIX_PATH", raising=False)
    with tempfile.TemporaryDirectory() as d:
        a = str(Path(d) / "a")
        b = str(Path(d) / "b")
        Path(a).mkdir(parents=True)
        Path(b).mkdir(parents=True)
        result = nanopynix_expr.parse_nix_path(f"foo={a}:bar={b}")
    assert result == [f"foo={a}", f"bar={b}"]


def test_parse_nix_path_url_style(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse_nix_path handles URL-style entries (scheme://...) without splitting on colons."""
    monkeypatch.setenv("NIX_PATH", "nixpkgs=https://github.com/NixOS/nixpkgs/archive/master.tar.gz")
    result = nanopynix_expr.parse_nix_path()
    assert len(result) == 1
    assert "://" in result[0]
    assert "nixpkgs=" in result[0]
