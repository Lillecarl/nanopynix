"""Tests for nanopynix_fetchers (Input, input_from_url, input_from_attrs)."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false
# nanopynix / nanopynix_fetchers are C++ nanobind extensions without type stubs.
# Fixture store parameter and variable types from extension calls are unresolvable.

from typing import Any

import pytest
from nanopynix_bindings import fetchers as nanopynix_fetchers

import nanopynix


class TestInputFromURL:
    def test_github_url(self):
        inp = nanopynix.input_from_url("github:NixOS/nixpkgs")
        assert isinstance(inp, nanopynix_fetchers.Input)
        s = str(inp)
        assert "github" in s or "NixOS" in s

    def test_github_url_with_ref(self):
        inp = nanopynix.input_from_url("github:NixOS/nixpkgs/nixos-24.11")
        assert isinstance(inp, nanopynix_fetchers.Input)

    def test_to_string(self):
        inp = nanopynix.input_from_url("github:NixOS/nixpkgs")
        assert isinstance(inp.to_string(), str)
        assert len(inp.to_string()) > 0

    def test_to_url_string(self):
        inp = nanopynix.input_from_url("github:NixOS/nixpkgs")
        url_str = inp.to_url_string()
        assert isinstance(url_str, str)
        assert len(url_str) > 0

    def test_repr(self):
        inp = nanopynix.input_from_url("github:NixOS/nixpkgs")
        r = repr(inp)
        assert r.startswith("Input(")

    def test_invalid_url_raises(self):
        with pytest.raises(RuntimeError):
            nanopynix.input_from_url("not-a-valid-scheme:foo")


class TestInputFromAttrs:
    def test_basic_attrs(self):
        attrs = {
            "type": "github",
            "owner": "NixOS",
            "repo": "nixpkgs",
        }
        inp = nanopynix.input_from_attrs(attrs)
        assert isinstance(inp, nanopynix_fetchers.Input)

    def test_to_attrs_roundtrip(self):
        attrs = {
            "type": "github",
            "owner": "NixOS",
            "repo": "nixpkgs",
            "ref": "nixos-24.11",
        }
        inp = nanopynix.input_from_attrs(attrs)
        out = inp.to_attrs()
        assert out["type"] == "github"
        assert out["owner"] == "NixOS"
        assert out["repo"] == "nixpkgs"

    def test_empty_attrs_raises(self):
        with pytest.raises(RuntimeError):
            nanopynix.input_from_attrs({})


class TestInputFingerprint:
    def test_get_fingerprint(self, store: Any) -> None:
        inp = nanopynix.input_from_url("github:NixOS/nixpkgs")
        fp = inp.get_fingerprint(store)
        if fp is not None:
            assert isinstance(fp, str)
            assert len(fp) > 0
