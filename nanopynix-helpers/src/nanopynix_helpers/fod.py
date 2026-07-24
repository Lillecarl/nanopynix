"""Conservative fixed-output derivation hash extraction and source updates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import tree_sitter_nix  # type: ignore[reportMissingTypeStubs] -- tree-sitter-nix does not ship type stubs
from tree_sitter import Language, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nanopynix.rpc import Store, ValueProxy

_HASH_ATTRIBUTES = frozenset({"hash", "sha256", "outputHash"})
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HASH = r"(?:md5|sha1|sha256|sha512)(?::[0-9a-z]+|-[A-Za-z0-9+/]+={0,2})"
_FOD_MISMATCH = re.compile(
    rf"^\s*(?:error:\s+)?hash mismatch in (?:fixed-output derivation '(?P<drv_path>[^']+)'|file downloaded from '[^']+'):\n"
    rf"\s*specified:\s+(?P<specified>{_HASH})\n"
    rf"\s*got:\s+(?P<got>{_HASH})$",
    re.MULTILINE,
)
_NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))  # type: ignore[reportDeprecated, reportUnknownMemberType] -- tree-sitter-nix 0.3 exposes the legacy integer language pointer

# An `apply_expression` node is always a binary (function, argument) application.
_APPLY_EXPRESSION_ARITY = 2


class FodSourceUpdateError(ValueError):
    """A fixed-output mismatch could not be mapped to one safe source literal."""


@dataclass(frozen=True)
class FodHashMismatch:
    """The specified and actual Nix hashes from one strict FOD mismatch message."""

    specified: str
    got: str
    drv_path: str | None


@dataclass(frozen=True)
class FodHashLiteral:
    """One plain-string fixed-output hash binding in a Nix source file."""

    attribute: str
    value: str
    start_byte: int
    end_byte: int
    derivation_name: str | None = None


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
    return FodHashMismatch(specified=match["specified"], got=match["got"], drv_path=match["drv_path"])


def extract_unique_fod_hash_mismatch(messages: Iterable[str]) -> FodHashMismatch | None:
    """Return one mismatch from log messages, refusing ambiguous diagnostics."""
    matches = [mismatch for message in messages if (mismatch := extract_fod_hash_mismatch(message)) is not None]
    return matches[0] if len(matches) == 1 else None


def find_fod_hash_literal(source: str, specified: str, *, derivation_name: str | None = None) -> FodHashLiteral:
    """Find the unambiguous plain-string hash literal for a failed FOD."""
    encoded = source.encode()
    candidates = _fod_hash_literals(encoded)
    exact = [candidate for candidate in candidates if candidate.value == specified]
    if len(exact) == 1:
        return exact[0]
    if derivation_name is not None:
        named = [candidate for candidate in candidates if candidate.derivation_name == derivation_name]
        if len(named) == 1:
            return named[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FodSourceUpdateError("no plain hash, sha256, or outputHash string literal found")
    raise FodSourceUpdateError(
        "multiple hash literals found; refusing to guess which one produced the failed derivation"
    )


def replace_fod_hash(source: str, literal: FodHashLiteral, got: str) -> str:
    """Replace exactly one plain-string FOD hash literal with Nix's reported hash."""
    if '"' in got or "\n" in got:
        raise FodSourceUpdateError("the computed hash is not safe to insert into a Nix string literal")
    encoded = source.encode()
    return (encoded[: literal.start_byte] + f'"{got}"'.encode() + encoded[literal.end_byte :]).decode()


async def fixed_output_derivations_in_closure(store: Store, root_drv_path: str) -> set[str]:
    """Return fixed-output derivations in *root_drv_path*'s input closure.

    Nix's evaluated derivation values intentionally expose their own
    ``drvAttrs`` but not derivations referenced through string context.  The
    latter are serialized as ``inputDrvs`` in the root derivation, so this is
    the earliest complete representation of its FOD dependencies.
    """
    pending = [root_drv_path]
    visited: set[str] = set()
    fixed_output: set[str] = set()
    while pending:
        drv_path = pending.pop()
        if drv_path in visited:
            continue
        visited.add(drv_path)
        derivation = await store.read_derivation(drv_path)
        if any(output.type == "CAFixed" for output in derivation.outputs.values()):
            fixed_output.add(drv_path)
        pending.extend(derivation.input_drvs)
    return fixed_output


async def evaluated_derivation_path(value: ValueProxy) -> str | None:
    """Return an evaluated derivation value's ``drvPath``, if it has one."""
    if not await value.has_attr("type"):
        return None
    if await value.attr("type").force_json() != "derivation":
        return None
    if not await value.has_attr("drvPath"):
        return None
    drv_path = await value.attr("drvPath").force_json()
    if not isinstance(drv_path, str):
        raise FodSourceUpdateError("evaluated derivation drvPath was not a string")
    return drv_path


async def mismatch_is_target_fod(store: Store, root: ValueProxy, mismatch: FodHashMismatch) -> bool:
    """Whether a daemon-reported FOD mismatch belongs to the evaluated target.

    ``builtins.fetchurl`` reports no derivation path, so it cannot be related
    to a root closure and retains the strict diagnostic-only fallback.  A
    fixed-output derivation diagnostic, however, must name a CAFixed
    derivation in the root closure before we patch source.
    """
    if mismatch.drv_path is None:
        return True
    root_drv_path = await evaluated_derivation_path(root)
    if root_drv_path is None:
        return False
    root_derivation = await store.read_derivation(root_drv_path)
    if any(output.type == "CAFixed" for output in root_derivation.outputs.values()):
        # Nix builds a resolved version of a FOD when its source expression
        # contains an empty hash.  Its diagnostic path is therefore distinct
        # from this evaluated ``drvPath``, although this direct target is the
        # derivation being checked.
        return True
    return await is_fixed_output_derivation_in_closure(store, root_drv_path, mismatch.drv_path)


async def is_fixed_output_derivation_in_closure(store: Store, root_drv_path: str, candidate_drv_path: str) -> bool:
    """Verify *candidate_drv_path* is a CAFixed member of *root_drv_path*'s closure.

    The build diagnostic normally provides the exact candidate path.  Empty
    hashes are resolved to a new derivation path during build, so in that case
    require its derivation name to identify exactly one CAFixed closure member.
    """
    pending = [root_drv_path]
    visited: set[str] = set()
    candidate_name = derivation_name_from_path(candidate_drv_path)
    name_matches = 0
    while pending:
        drv_path = pending.pop()
        if drv_path in visited:
            continue
        visited.add(drv_path)
        derivation = await store.read_derivation(drv_path)
        if drv_path == candidate_drv_path:
            return any(output.type == "CAFixed" for output in derivation.outputs.values())
        if candidate_name is not None and derivation.name == candidate_name:
            if not any(output.type == "CAFixed" for output in derivation.outputs.values()):
                return False
            name_matches += 1
        pending.extend(derivation.input_drvs)
    return name_matches == 1


def derivation_name_from_path(drv_path: str | None) -> str | None:
    """Return the Nix derivation name embedded in a store ``.drv`` path."""
    if drv_path is None:
        return None
    base_name = drv_path.rsplit("/", 1)[-1]
    hash_separator = base_name.find("-")
    if hash_separator < 0 or not base_name.endswith(".drv"):
        return None
    return base_name[hash_separator + 1 : -len(".drv")]


def _fod_hash_literals(source: bytes) -> list[FodHashLiteral]:
    root = Parser(_NIX_LANGUAGE).parse(source).root_node
    run_command_bindings = _run_command_hash_bindings(root, source)
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
        literals.append(
            FodHashLiteral(
                attribute,
                rendered[1:-1].decode(),
                string.start_byte,
                string.end_byte,
                run_command_bindings.get(node.start_byte),
            ),
        )
    return literals


def _run_command_hash_bindings(root: Any, source: bytes) -> dict[int, str]:  # noqa: C901 tracked complexity/arg-count debt, see TODO.md
    """Return direct output-hash bindings of ``runCommand NAME { ... }`` calls."""
    names: dict[int, str] = {}
    nodes: list[Any] = [root]
    while nodes:
        node = nodes.pop()
        nodes.extend(reversed(node.children))
        if node.type != "apply_expression" or len(node.named_children) != _APPLY_EXPRESSION_ARITY:
            continue
        function, attrs = node.named_children
        if attrs.type != "attrset_expression" or function.type != "apply_expression":
            continue
        if len(function.named_children) != _APPLY_EXPRESSION_ARITY:
            continue
        callee, name = function.named_children
        if name.type != "string_expression" or any(child.type == "interpolation" for child in name.children):
            continue
        callee_text = source[callee.start_byte : callee.end_byte]
        rendered_name = source[name.start_byte : name.end_byte]
        if (
            not callee_text.endswith(b"runCommand")
            or not rendered_name.startswith(b'"')
            or not rendered_name.endswith(b'"')
        ):
            continue
        derivation_name = rendered_name[1:-1].decode()
        binding_set = next((child for child in attrs.named_children if child.type == "binding_set"), None)
        if binding_set is None:
            continue
        for binding in binding_set.named_children:
            if binding.type != "binding":
                continue
            attrpath = next((child for child in binding.named_children if child.type == "attrpath"), None)
            if attrpath is None or source[attrpath.start_byte : attrpath.end_byte] != b"outputHash":
                continue
            names[binding.start_byte] = derivation_name
    return names
