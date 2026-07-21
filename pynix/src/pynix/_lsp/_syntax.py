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
        select = parent.parent
        if select is None or select.type != "select_expression":
            return None
        base = select.child_by_field_name("expression")
        if base is None or base.type != "variable_expression":
            return None
        base_name_node = base.child_by_field_name("name")
        if base_name_node is None:
            return None
        path = [text(base_name_node)]
        for attr in parent.named_children:
            if attr.type != "identifier":
                continue
            path.append(text(attr))
            if attr.end_byte >= identifier.end_byte:
                break
        return path

    return None


_ATTRPATH_TAIL_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_'.-])((?:[A-Za-z_][A-Za-z0-9_'-]*\.)*)([A-Za-z_][A-Za-z0-9_'-]*)?$"
)


def completion_target_at(source: str, byte_offset: int) -> tuple[list[str], str] | None:
    """Return ``(prefix_path, partial)`` for attribute completion at *byte_offset*.

    ``prefix_path`` is the dotted path before the segment currently being
    typed; ``partial`` is that (possibly empty) in-progress segment. E.g.
    for ``cfg.serv`` with the cursor at the end, returns
    ``(["cfg"], "serv")``. Returns None if there's no identifier chain
    immediately before the cursor.
    """
    text_before = source.encode()[:byte_offset].decode()
    match = _ATTRPATH_TAIL_RE.search(text_before)
    if match is None:
        return None
    prefix_dotted, partial = match.groups()
    prefix = [segment for segment in (prefix_dotted or "").split(".") if segment]
    if not prefix and not partial:
        return None
    return prefix, partial or ""


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
