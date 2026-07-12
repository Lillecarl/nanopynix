"""Tests for NIX_PATH support (EvalSettings::parseNixPath via nanopynix_expr.parse_nix_path)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def test_parse_nix_path_empty(monkeypatch):
    """_parse_nix_path returns [] when NIX_PATH is unset."""
    import nanopynix_expr

    monkeypatch.delenv("NIX_PATH", raising=False)
    result = nanopynix_expr.parse_nix_path()
    assert result == []


def test_parse_nix_path_simple(monkeypatch):
    """parse_nix_path splits colon-separated prefix=path entries."""
    import nanopynix_expr

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


def test_parse_nix_path_url_style():
    """parse_nix_path handles URL-style entries (scheme://...) without splitting on colons."""
    import nanopynix_expr

    os.environ["NIX_PATH"] = "nixpkgs=https://github.com/NixOS/nixpkgs/archive/master.tar.gz"
    try:
        result = nanopynix_expr.parse_nix_path()
    finally:
        del os.environ["NIX_PATH"]
    assert len(result) == 1
    assert "://" in result[0]
    assert "nixpkgs=" in result[0]
