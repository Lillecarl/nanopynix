"""Conservative fixed-output derivation hash extraction and source updates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import tree_sitter_nix  # type: ignore[reportMissingTypeStubs] -- tree-sitter-nix does not ship type stubs
from tree_sitter import Language, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable

_HASH_ATTRIBUTES = frozenset({"hash", "sha256", "outputHash"})
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HASH = r"(?:md5|sha1|sha256|sha512)(?::[0-9a-z]+|-[A-Za-z0-9+/]+={0,2})"
_FOD_MISMATCH = re.compile(
    rf"^\s*(?:error:\s+)?hash mismatch in (?:fixed-output derivation '[^']+'|file downloaded from '[^']+'):\n"
    rf"\s*specified:\s+(?P<specified>{_HASH})\n"
    rf"\s*got:\s+(?P<got>{_HASH})$",
    re.MULTILINE,
)
_NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))  # type: ignore[reportDeprecated, reportUnknownMemberType] -- tree-sitter-nix 0.3 exposes the legacy integer language pointer


class FodSourceUpdateError(ValueError):
    """A fixed-output mismatch could not be mapped to one safe source literal."""


@dataclass(frozen=True)
class FodHashMismatch:
    """The specified and actual Nix hashes from one strict FOD mismatch message."""

    specified: str
    got: str


@dataclass(frozen=True)
class FodHashLiteral:
    """One plain-string fixed-output hash binding in a Nix source file."""

    attribute: str
    value: str
    start_byte: int
    end_byte: int


def extract_fod_hash_mismatch(message: str) -> FodHashMismatch | None:
    """Extract a FOD mismatch only from Nix's exact two-line diagnostic shape.

    TODO: Replace this fallback with a structured Nix API. One promising route is
    evaluating the target, walking its derivation closure, filtering unbuilt
    FODs, and building only those in a temporary chroot store where their
    output metadata can be observed directly.
    """
    matches = list(_FOD_MISMATCH.finditer(_ANSI_ESCAPE.sub("", message)))
    if len(matches) != 1:
        return None
    match = matches[0]
    return FodHashMismatch(specified=match["specified"], got=match["got"])


def extract_unique_fod_hash_mismatch(messages: Iterable[str]) -> FodHashMismatch | None:
    """Return one mismatch from log messages, refusing ambiguous diagnostics."""
    matches = [mismatch for message in messages if (mismatch := extract_fod_hash_mismatch(message)) is not None]
    return matches[0] if len(matches) == 1 else None


def find_fod_hash_literal(source: str, specified: str) -> FodHashLiteral:
    """Find the unambiguous plain-string hash literal for a failed FOD."""
    encoded = source.encode()
    candidates = _fod_hash_literals(encoded)
    exact = [candidate for candidate in candidates if candidate.value == specified]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FodSourceUpdateError("no plain hash, sha256, or outputHash string literal found")
    raise FodSourceUpdateError("multiple hash literals found; refusing to guess which one produced the failed derivation")


def replace_fod_hash(source: str, literal: FodHashLiteral, got: str) -> str:
    """Replace exactly one plain-string FOD hash literal with Nix's reported hash."""
    if '"' in got or "\n" in got:
        raise FodSourceUpdateError("the computed hash is not safe to insert into a Nix string literal")
    encoded = source.encode()
    return (encoded[: literal.start_byte] + f'"{got}"'.encode() + encoded[literal.end_byte :]).decode()


def _fod_hash_literals(source: bytes) -> list[FodHashLiteral]:
    root = Parser(_NIX_LANGUAGE).parse(source).root_node
    literals: list[FodHashLiteral] = []
    nodes: list[Any] = [root]
    while nodes:
        node = nodes.pop()
        nodes.extend(reversed(node.children))
        if node.type != "binding":
            continue
        attrpath = next((child for child in node.named_children if child.type == "attrpath"), None)
        string = next((child for child in node.named_children if child.type == "string_expression"), None)
        if attrpath is None or string is None or any(child.type == "interpolation" for child in string.children):
            continue
        attribute = source[attrpath.start_byte : attrpath.end_byte].decode()
        rendered = source[string.start_byte : string.end_byte]
        if attribute not in _HASH_ATTRIBUTES or not rendered.startswith(b'"') or not rendered.endswith(b'"'):
            continue
        literals.append(FodHashLiteral(attribute, rendered[1:-1].decode(), string.start_byte, string.end_byte))
    return literals
