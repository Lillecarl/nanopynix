"""Unit tests for pynixd.types.path_info — ValidPathInfo narinfo serialization.

Tests from_narinfo parsing and to_narinfo serialization with various
combinations of fields. All tests are pure — no I/O.
"""

from __future__ import annotations

import pytest

from pynixd.store_path import StorePath
from pynixd.types.path_info import ValidPathInfo
from tests.test_features import TestFeatures as F


@pytest.mark.covers(F.PATH_INFO)
class TestNarinfoRoundtrip:
    def test_minimal(self):
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            nar_hash="sha256:abc123",
            nar_size=42,
        )
        narinfo = info.to_narinfo()
        parsed = ValidPathInfo.from_narinfo(narinfo)
        assert parsed.path == info.path
        assert parsed.nar_hash == info.nar_hash
        assert parsed.nar_size == info.nar_size
        assert parsed.references == set()
        assert parsed.deriver == StorePath("")
        assert parsed.sigs == set()
        assert parsed.ca == ""

    def test_with_references(self):
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            nar_hash="sha256:abc123",
            nar_size=100,
            references={
                StorePath("/nix/store/ref1-bar"),
                StorePath("/nix/store/ref2-baz"),
            },
        )
        narinfo = info.to_narinfo()
        parsed = ValidPathInfo.from_narinfo(narinfo)
        assert parsed.path == info.path
        assert parsed.references == info.references

    def test_with_deriver(self):
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            nar_hash="sha256:abc123",
            nar_size=50,
            deriver=StorePath("/nix/store/drv1-bar.drv"),
        )
        narinfo = info.to_narinfo()
        parsed = ValidPathInfo.from_narinfo(narinfo)
        assert parsed.deriver == info.deriver

    def test_with_signatures(self):
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            nar_hash="sha256:abc123",
            nar_size=50,
            sigs={"key1:sig1", "key2:sig2"},
        )
        narinfo = info.to_narinfo()
        parsed = ValidPathInfo.from_narinfo(narinfo)
        assert parsed.sigs == info.sigs

    def test_with_ca(self):
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            nar_hash="sha256:abc123",
            nar_size=50,
            ca="text:sha256:xyz",
        )
        narinfo = info.to_narinfo()
        parsed = ValidPathInfo.from_narinfo(narinfo)
        assert parsed.ca == info.ca

    def test_full_roundtrip(self):
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            deriver=StorePath("/nix/store/drv1-bar.drv"),
            nar_hash="sha256:abc123def456",
            references={
                StorePath("/nix/store/ref1-bar"),
                StorePath("/nix/store/ref2-baz"),
            },
            registration_time=12345,
            nar_size=999,
            ultimate=1,
            sigs={"key1:sig1", "key2:sig2"},
            ca="text:sha256:xyz",
        )
        narinfo = info.to_narinfo()
        parsed = ValidPathInfo.from_narinfo(narinfo)
        assert parsed.path == info.path
        assert parsed.deriver == info.deriver
        assert parsed.nar_hash == info.nar_hash
        assert parsed.references == info.references
        assert parsed.nar_size == info.nar_size
        assert parsed.sigs == info.sigs
        assert parsed.ca == info.ca

    def test_nar_hash_without_prefix(self):
        """to_narinfo should add sha256: prefix if missing."""
        info = ValidPathInfo(
            path=StorePath("/nix/store/abc123-foo"),
            nar_hash="abc123",
            nar_size=50,
        )
        narinfo = info.to_narinfo()
        # The roundtrip should add sha256: prefix
        assert "sha256:abc123" in narinfo


class TestNarinfoParsingEdgeCases:
    def test_empty_content(self):
        parsed = ValidPathInfo.from_narinfo("")
        assert parsed.path == StorePath("")

    def test_comment_lines(self):
        content = """# This is a comment
StorePath: /nix/store/abc-foo
NarHash: sha256:xyz
NarSize: 10
"""
        parsed = ValidPathInfo.from_narinfo(content)
        assert parsed.path == StorePath("/nix/store/abc-foo")

    def test_empty_lines(self):
        content = """StorePath: /nix/store/abc-foo

NarHash: sha256:xyz

NarSize: 10
"""
        parsed = ValidPathInfo.from_narinfo(content)
        assert parsed.path == StorePath("/nix/store/abc-foo")

    def test_references_without_prefix(self):
        """References without /nix/store/ prefix should be fixed up."""
        content = """StorePath: /nix/store/abc-foo
NarHash: sha256:xyz
NarSize: 10
References: ref1-bar ref2-baz
"""
        parsed = ValidPathInfo.from_narinfo(content)
        assert StorePath("/nix/store/ref1-bar") in parsed.references

    def test_deriver_without_prefix(self):
        content = """StorePath: /nix/store/abc-foo
NarHash: sha256:xyz
NarSize: 10
Deriver: drv1-bar.drv
"""
        parsed = ValidPathInfo.from_narinfo(content)
        assert parsed.deriver == StorePath("/nix/store/drv1-bar.drv")

    def test_empty_deriver(self):
        content = """StorePath: /nix/store/abc-foo
NarHash: sha256:xyz
NarSize: 10
Deriver:
"""
        parsed = ValidPathInfo.from_narinfo(content)
        assert parsed.deriver == StorePath("")

    def test_unknown_keys_ignored(self):
        content = """StorePath: /nix/store/abc-foo
NarHash: sha256:xyz
NarSize: 10
URL: nar/xyz.nar
Compression: none
"""
        parsed = ValidPathInfo.from_narinfo(content)
        assert parsed.path == StorePath("/nix/store/abc-foo")
        assert parsed.nar_hash == "sha256:xyz"
