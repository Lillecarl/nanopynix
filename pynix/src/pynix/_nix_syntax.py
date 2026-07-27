"""Shared tree-sitter-nix grammar setup for pynix's REPL and language server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import tree_sitter_nix  # type: ignore[reportMissingTypeStubs] -- tree-sitter-nix does not ship type stubs
from tree_sitter import Language, Parser

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from tree_sitter import Tree


class _Grammar(Protocol):
    path: str | None


class _GrammarConfig(Protocol):
    grammars: list[_Grammar]


NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))  # type: ignore[reportDeprecated, reportUnknownMemberType] -- tree-sitter-nix 0.3 exposes the legacy integer language pointer
_NIX_GRAMMAR_CONFIG = cast("_GrammarConfig | None", tree_sitter_nix.config())  # type: ignore[reportUnknownMemberType] -- tree-sitter-nix has no type stubs
if _NIX_GRAMMAR_CONFIG is None:
    raise RuntimeError("tree-sitter-nix did not provide its grammar configuration")
_grammar_path = _NIX_GRAMMAR_CONFIG.grammars[0].path
if _grammar_path is None:
    raise RuntimeError("tree-sitter-nix did not provide its grammar path")
NIX_GRAMMAR_PATH: str = _grammar_path


def parse_nix(source: str) -> Tree:
    """Parse one Nix source string with the shared tree-sitter-nix grammar."""
    return Parser(NIX_LANGUAGE).parse(source.encode())
