"""Tests for EvalSettings::isPseudoUrl, through nanopynix.is_pseudo_url.

This predicate decides the first branch of `lookup_file_arg`, which is what
`EvalState.file` applies to its argument. A caller that classifies such an
argument itself -- `pynix.target.resolve_file_reference` is the one -- asks
this instead of repeating the list of schemes, so these tests pin the list
that the caller relies on.
"""

from __future__ import annotations

import pytest

import nanopynix

# Every scheme that eval-settings.cc names, and the `channel:` prefix that it
# handles before it looks for a scheme at all.
PSEUDO_URLS = [
    "channel:nixos-unstable",
    "http://example.com/x.tar.gz",
    "https://example.com/x.tar.gz",
    "file:///tmp/x.tar.gz",
    "channel://example.com/x",
    "git://example.com/x",
    "s3://bucket/x.tar.gz",
    "ssh://example.com/x",
]

# Each of these reaches a later branch of `lookup_file_arg`, or no branch of
# it at all. A flake reference is the interesting group: it carries a scheme,
# and Nix does not download it as a tarball.
NOT_PSEUDO_URLS = [
    "github:NixOS/nixpkgs",
    "gitlab:owner/repo",
    "git+https://example.com/x",
    "git+ssh://example.com/x",
    "path:/tmp/tree",
    "flake:nixpkgs",
    "nixpkgs",
    "nixpkgs/nixos-25.05",
    "./default.nix",
    "/etc/nixos/configuration.nix",
    "<nixpkgs>",
    "",
    "ftp://example.com/x.tar.gz",
]


@pytest.mark.parametrize("value", PSEUDO_URLS)
def test_pseudo_url(value: str) -> None:
    assert nanopynix.is_pseudo_url(value)


@pytest.mark.parametrize("value", NOT_PSEUDO_URLS)
def test_not_a_pseudo_url(value: str) -> None:
    assert not nanopynix.is_pseudo_url(value)
