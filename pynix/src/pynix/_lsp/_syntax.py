"""tree-sitter-nix-backed syntax analysis for pynix's language server.

Two different strategies are used deliberately:

- Diagnostics and hover work against a real tree-sitter parse tree, since
  they act on code the user has already finished writing.
- Completion works against a plain lexical scan of the text immediately
  before the cursor, since completion by definition happens mid-edit, where
  the surrounding code is often not yet grammatically valid (e.g. a
  trailing ``.`` with nothing after it) and tree-sitter's error-recovery
  nodes are more fragile to special-case than just matching the identifier
  chain being typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pynix._completion import completion_prefix_at
from pynix._nix_syntax import parse_nix


@dataclass(frozen=True)
class ParseErrorRange:
    """One tree-sitter ERROR/MISSING node, in (row, column) points."""

    start_row: int
    start_column: int
    end_row: int
    end_column: int
    message: str


def parse_errors(source: str) -> list[ParseErrorRange]:
    """Return every ERROR/MISSING node in a fresh parse of *source*."""
    tree = parse_nix(source)
    errors: list[ParseErrorRange] = []
    stack: list[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.has_error and (node.type == "ERROR" or node.is_missing):
            message = "missing syntax" if node.is_missing else "syntax error"
            errors.append(
                ParseErrorRange(
                    node.start_point[0],
                    node.start_point[1],
                    node.end_point[0],
                    node.end_point[1],
                    message,
                )
            )
            continue
        stack.extend(node.children)
    return errors


def identifier_path_at(source: str, byte_offset: int) -> list[str] | None:
    """Return the dotted attribute path containing the identifier at *byte_offset*.

    E.g. with the cursor on the ``b`` in ``a.b.c``, returns ``["a", "b"]`` --
    the path from the expression's root up to and including the segment
    under the cursor. Returns a single-element list for a bare variable
    reference with no ``.``. Returns None if the cursor isn't positioned on
    a plain ``name.name.name``-shaped expression (e.g. it's on an operator,
    a string, or a more complex base expression).

    Also handles a *binding*'s own attrpath (e.g. the ``services.foo`` in
    ``services.foo = true;``) -- an attribute-definition key rather than a
    reference, which has no base variable to anchor on, so the returned path
    starts directly at that binding's own first segment. This only looks at
    the enclosing binding's own attrpath; it does not walk up through
    parent bindings for a nested-attrset-style definition like
    ``services = { foo = true; };``.
    """
    tree = parse_nix(source)
    node = tree.root_node.descendant_for_byte_range(byte_offset, byte_offset)
    if node is None:
        return None

    encoded = source.encode()

    def text(n: Any) -> str:
        return encoded[n.start_byte : n.end_byte].decode()

    identifier = node if node.type == "identifier" else None
    if identifier is None and node.parent is not None and node.parent.type == "identifier":
        identifier = node.parent
    if identifier is None:
        if node.type == "variable_expression":
            name_node = node.child_by_field_name("name")
            return None if name_node is None else [text(name_node)]
        return None

    parent = identifier.parent
    if parent is None:
        return [text(identifier)]

    if parent.type == "variable_expression":
        return [text(identifier)]

    if parent.type == "attrpath":
        select_or_binding = parent.parent
        if select_or_binding is None:
            return None
        if select_or_binding.type == "select_expression":
            base = select_or_binding.child_by_field_name("expression")
            if base is None or base.type != "variable_expression":
                return None
            base_name_node = base.child_by_field_name("name")
            if base_name_node is None:
                return None
            path = [text(base_name_node)]
        elif select_or_binding.type == "binding":
            path = []
        else:
            return None
        for attr in parent.named_children:
            if attr.type != "identifier":
                continue
            path.append(text(attr))
            if attr.end_byte >= identifier.end_byte:
                break
        return path

    return None


_IDENTIFIER_CHAIN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*(?:\.[A-Za-z_][A-Za-z0-9_'-]*)*")


def completion_target_at(source: str, byte_offset: int) -> tuple[list[str], str] | None:
    """Return ``(prefix_path, partial)`` for attribute completion at *byte_offset*.

    ``prefix_path`` is the dotted path before the segment currently being
    typed; ``partial`` is that (possibly empty) in-progress segment. E.g.
    for ``cfg.serv`` with the cursor at the end, returns
    ``(["cfg"], "serv")``. Returns None if there's no identifier chain
    immediately before the cursor.

    A thin adapter over ``completion_prefix_at`` (shared with the REPL,
    which can also complete after arbitrary non-identifier expressions):
    this server only ever resolves plain dotted-identifier chains against
    its named roots, so a non-``None`` prefix that isn't shaped like one
    (e.g. it came from completing after ``(import ./foo.nix).ba``) can't be
    used here and yields None, same as it always could not.
    """
    result = completion_prefix_at(source, byte_offset)
    if result is None:
        return None
    prefix_text, partial = result
    if prefix_text is None:
        prefix: list[str] = []
    elif _IDENTIFIER_CHAIN_RE.fullmatch(prefix_text):
        prefix = prefix_text.split(".")
    else:
        return None
    if not prefix and not partial:
        return None
    return prefix, partial


def top_level_lambda_formals(source: str) -> list[str] | None:
    """Return the file's own outermost lambda's declared formal names.

    NixOS modules are conventionally ``{ config, pkgs, lib, ... }: { ... }``.
    This is used to decide which NixOS module-system arguments (see
    ``_handlers.py``'s ``resolve_module_arg``) a file is even allowed to
    reference -- offering ``pkgs.``-completion in a file whose own lambda
    never took a ``pkgs`` argument would suggest a name that isn't actually
    in scope. Only inspects the file's own top-level function, not any
    nested one. Returns None if the top level isn't a ``{ ... }:``-shaped
    function at all (e.g. a plain attrset, or a single-identifier lambda
    like ``x: ...``, which NixOS modules don't use).
    """
    tree = parse_nix(source)
    encoded = source.encode()
    fn = next((child for child in tree.root_node.children if child.type == "function_expression"), None)
    if fn is None:
        return None
    formals = fn.child_by_field_name("formals")
    if formals is None:
        return None
    names: list[str] = []
    for formal in formals.named_children:
        if formal.type != "formal":
            continue
        name_node = formal.child_by_field_name("name")
        if name_node is not None:
            names.append(encoded[name_node.start_byte : name_node.end_byte].decode())
    return names


@dataclass(frozen=True)
class SymbolRange:
    """One top-level binding's attribute path and source range, in points."""

    path: str
    start_row: int
    start_column: int
    end_row: int
    end_column: int


def top_level_symbols(source: str) -> list[SymbolRange]:
    """Return every attrset binding directly under the file's own bindings.

    Walks all ``binding`` nodes in source order (not just the outermost
    attrset) -- for a typical NixOS module (a function returning one nested
    attrset), this surfaces every ``options.a.b.c``/``config.x.y`` binding
    as a flat outline, which is more useful here than a strict one-level
    top-down tree.
    """
    tree = parse_nix(source)
    encoded = source.encode()
    symbols: list[SymbolRange] = []
    stack: list[Any] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "binding":
            attrpath = node.child_by_field_name("attrpath")
            if attrpath is not None:
                path_text = encoded[attrpath.start_byte : attrpath.end_byte].decode()
                symbols.append(
                    SymbolRange(
                        path_text,
                        node.start_point[0],
                        node.start_point[1],
                        node.end_point[0],
                        node.end_point[1],
                    )
                )
        stack.extend(node.children)
    symbols.sort(key=lambda symbol: (symbol.start_row, symbol.start_column))
    return symbols
