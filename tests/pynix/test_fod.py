"""Tests for fixed-output derivation source updates."""

from __future__ import annotations

import pytest
from pynix.fod import (
    FodSourceUpdateError,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    replace_fod_hash,
)


def test_extracts_only_the_exact_ansi_colored_nix_fod_shape() -> None:
    mismatch = extract_fod_hash_mismatch(
        "error: hash mismatch in fixed-output derivation '\x1b[35;1m/nix/store/source.drv\x1b[0m':\n"
        "  specified: \x1b[35;1msha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\x1b[0m\n"
        "     got:    \x1b[35;1msha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=\x1b[0m"
    )

    assert mismatch is not None
    assert mismatch.got == "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="
    assert extract_fod_hash_mismatch("hash mismatch in an unrelated format") is None


def test_extract_unique_fod_hash_mismatch_rejects_multiple_events() -> None:
    first = (
        "error: hash mismatch in fixed-output derivation '/nix/store/a.drv':\n"
        "  specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
        "  got: sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    )
    second = first.replace("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")

    assert extract_unique_fod_hash_mismatch([first]) == extract_fod_hash_mismatch(first)
    assert extract_unique_fod_hash_mismatch([first, second]) is None


def test_replaces_one_plain_hash_literal() -> None:
    source = 'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = ""; }\n'
    literal = find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    assert replace_fod_hash(source, literal, "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4=") == (
        'pkgs.fetchFromGitHub { owner = "lillecarl"; hash = "sha256-XG19bBLOoknhsnwV5rVaVGB8DYUiNPMklhyNotZNcD4="; }\n'
    )


def test_refuses_ambiguous_hash_literals() -> None:
    source = '{ first = { hash = ""; }; second = { sha256 = ""; }; }\n'

    with pytest.raises(FodSourceUpdateError, match="multiple hash literals"):
        find_fod_hash_literal(source, "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
