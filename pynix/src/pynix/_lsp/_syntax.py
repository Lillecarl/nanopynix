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

    Tries *byte_offset* first, then falls back to *byte_offset - 1*: the same
    token-boundary quirk fixed in ``_string_fragment_at`` applies here too --
    a cursor right after an identifier the user just finished typing (e.g.
    right after ``count`` in ``count = 1;``, before the following space)
    resolves to the enclosing ``binding`` node, not the ``identifier``, since
    ``descendant_for_byte_range`` favors the token *following* a boundary
    point. The fallback only fires when the primary lookup finds nothing, so
    it can't misfire for the (much more common) cursor-inside-a-token case,
    which already succeeds on the first try.
    """
    tree = parse_nix(source)
    result = _identifier_path_at_node(source, tree.root_node.descendant_for_byte_range(byte_offset, byte_offset))
    if result is not None or byte_offset == 0:
        return result
    return _identifier_path_at_node(
        source, tree.root_node.descendant_for_byte_range(byte_offset - 1, byte_offset - 1)
    )


def enclosing_binding_path_at(source: str, byte_offset: int) -> list[str]:
    """Return the dotted path of every attrset-valued binding enclosing *byte_offset*, outermost first.

    Unlike ``identifier_path_at`` (which only ever resolves the *innermost*
    binding's own flat attrpath, by design -- see its own docstring),
    climbing here means a bare or partially-typed key nested several
    ``binding -> attrset_expression -> binding_set`` layers deep still
    resolves against the full path leading to it. E.g. for the cursor on a
    not-yet-typed key inside::

        kubernetes.objects.default.Deployment.demo = {
          spec = {
            <cursor>
          };
        };

    returns ``["kubernetes", "objects", "default", "Deployment", "demo",
    "spec"]`` -- combine with ``identifier_path_at``/``completion_target_at``'s
    own (local-only) result for the full path, since neither of those two
    know about this outer nesting on their own.

    A bare, single-segment ``config = { ... };`` wrapping the file's own
    top-level attrset is transparently skipped -- NixOS's module convention
    treats that wrapper as equivalent to not wrapping at all (see
    ``_terranix.py``'s own ``config``-wrapper regression test), so climbing
    through it must not surface a spurious leading ``"config"`` segment.
    Only the true top-level wrapper is special-cased this way: a *nested*
    attribute that happens to be named ``config`` several layers deep (e.g.
    a Kubernetes ConfigMap's own ``data.config`` field, written in nested
    style) is a perfectly ordinary segment and is never skipped, since its
    own enclosing chain has further bindings above it.

    Returns ``[]`` when *byte_offset* isn't inside any attrset that is
    itself the value of a binding (e.g. a fully flat top-level binding like
    ``a.b.c = 1;``, or the file's own top-level attrset) -- callers can
    always unconditionally prepend this result with no special-casing for
    "no enclosing binding" vs "some enclosing binding".
    """
    tree = parse_nix(source)
    encoded = source.encode()

    def text(n: Any) -> str:
        return encoded[n.start_byte : n.end_byte].decode()

    node = tree.root_node.descendant_for_byte_range(byte_offset, byte_offset)
    segments: list[list[str]] = []
    current = node
    while current is not None:
        if current.type == "attrset_expression":
            parent = current.parent
            if parent is not None and parent.type == "binding":
                attrpath_node = parent.child_by_field_name("attrpath")
                segment = (
                    [text(child) for child in attrpath_node.named_children if child.type == "identifier"]
                    if attrpath_node is not None
                    else []
                )
                binding_set = parent.parent
                enclosing_attrset = binding_set.parent if binding_set is not None else None
                is_top_level_config_wrapper = (
                    segment == ["config"]
                    and enclosing_attrset is not None
                    and enclosing_attrset.type == "attrset_expression"
                    and (enclosing_attrset.parent is None or enclosing_attrset.parent.type != "binding")
                )
                if not is_top_level_config_wrapper:
                    segments.append(segment)
                current = parent
                continue
        current = current.parent
    segments.reverse()
    return [segment for group in segments for segment in group]


def _identifier_path_at_node(source: str, node: Any | None) -> list[str] | None:
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
_DOTTED_CHAIN_FULLMATCH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def _string_fragment_at(source: str, byte_offset: int) -> Any | None:
    """Return the sole ``string_fragment`` node containing *byte_offset*, if any.

    ``descendant_for_byte_range`` resolves a zero-width point sitting exactly
    on a token boundary to whichever token follows it -- so a cursor right
    after the last character the user just typed (immediately before the
    closing ``"``) resolves to the closing-quote token, not the
    ``string_fragment`` a moment ago. That's the single most common
    completion position there is (type some characters, cursor sits right
    after them), so this climbs from whatever node the point query returns up
    to its enclosing ``string_expression`` and then locates the fragment by
    byte range instead of trusting the point query's own node choice.
    """
    tree = parse_nix(source)
    node = tree.root_node.descendant_for_byte_range(byte_offset, byte_offset)
    string_expression = node
    while string_expression is not None and string_expression.type != "string_expression":
        string_expression = string_expression.parent
    if string_expression is None:
        return None
    fragments = [child for child in string_expression.children if child.type == "string_fragment"]
    if len(fragments) != 1:
        return None
    fragment = fragments[0]
    if not (fragment.start_byte <= byte_offset <= fragment.end_byte):
        return None
    return fragment


def string_literal_path_at(source: str, byte_offset: int) -> list[str] | None:
    """Return the dotted path inside a plain string literal, truncated at *byte_offset*.

    E.g. with the cursor on ``random_id`` in ``"random_id.suffix.hex"``,
    returns ``["random_id"]``; on ``suffix``, ``["random_id", "suffix"]`` --
    mirrors ``identifier_path_at``'s own truncate-at-cursor behavior for a
    plain (non-string) attribute path, so a caller can't tell the two apart
    by shape. Dialect-agnostic: only recognizes the shape (a single,
    non-interpolated string fragment whose whole content looks like
    ``name.name.name``), with no knowledge of what any particular dialect's
    convention (e.g. terranix's ``lib.tfRef "..."``) does with it. Returns
    None if the cursor isn't inside such a string, or the fragment has an
    interpolation (``string_expression`` would then have more than one child
    between the quotes) or isn't dotted-identifier-shaped.
    """
    fragment = _string_fragment_at(source, byte_offset)
    if fragment is None:
        return None
    encoded = source.encode()
    text = encoded[fragment.start_byte : fragment.end_byte].decode()
    if not _DOTTED_CHAIN_FULLMATCH_RE.fullmatch(text):
        return None
    segments = text.split(".")
    local_offset = byte_offset - fragment.start_byte
    cursor = 0
    for index, segment in enumerate(segments):
        cursor += len(segment.encode())
        if local_offset <= cursor:
            return segments[: index + 1]
        cursor += 1  # the "." separator
    return segments


def string_literal_completion_target_at(source: str, byte_offset: int) -> tuple[list[str], str] | None:
    """Return ``(prefix_path, partial)`` for completion inside a plain string literal.

    Mirrors ``completion_target_at``, but for the text between quotes rather
    than a bare identifier chain -- e.g. with the cursor right after
    ``"random_id.suf`` inside ``lib.tfRef "random_id.suf..."``, returns
    ``(["random_id"], "suf")``. Same shape restriction as
    ``string_literal_path_at`` (a single, non-interpolated string fragment),
    but unlike that function, only looks at the text *before* the cursor --
    completion is inherently about an in-progress, not-yet-finished token, so
    text after the cursor (e.g. the rest of an already-typed attribute name)
    is irrelevant here. Dialect-agnostic, same as ``string_literal_path_at``.
    """
    fragment = _string_fragment_at(source, byte_offset)
    if fragment is None:
        return None
    encoded = source.encode()
    text_before_cursor = encoded[fragment.start_byte : byte_offset].decode()
    if text_before_cursor and not _DOTTED_CHAIN_FULLMATCH_RE.fullmatch(text_before_cursor):
        return None
    segments = text_before_cursor.split(".")
    partial = segments[-1]
    prefix = segments[:-1]
    if not prefix and not partial:
        return None
    return prefix, partial


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


def _direct_bindings(node: Any) -> list[Any]:
    """Every ``binding`` node reachable from *node* without passing through another ``binding``.

    Stopping the walk the moment a ``binding`` node is found (rather than
    descending into its subtree) is what makes this "one level" rather than
    "every binding anywhere below" -- intervening wrapper nodes (a
    function's body, ``binding_set`` itself, parens) are transparently
    walked through since none of them are themselves type ``"binding"``.
    """
    bindings: list[Any] = []
    stack: list[Any] = list(node.children)
    while stack:
        candidate = stack.pop()
        if candidate.type == "binding":
            bindings.append(candidate)
            continue
        stack.extend(candidate.children)
    return bindings


def attrpath_range(source: str, path: tuple[str, ...]) -> tuple[int, int, int, int] | None:
    """Return the ``(start_row, start_column, end_row, end_column)`` span of the binding at *path*.

    Handles both a single flat dotted binding (``a.b.c = ...;``) and nested
    attrset-valued bindings (``a.b = { c = ...; };``, the shape this
    repository's own terranix fixtures actually use) uniformly: each
    binding's own ``attrpath`` is matched segment-by-segment against a
    prefix of *path*, recursing into an attrset-valued binding's own nested
    bindings for the remainder. Returns None rather than raising when *path*
    isn't reachable this way (e.g. built via a ``//`` merge, ``inherit``, or
    a computed/non-identifier attrpath segment) -- callers fall back to a
    whole-file position.
    """
    if not path:
        return None
    tree = parse_nix(source)
    encoded = source.encode()

    def segments_of(attrpath_node: Any) -> list[str] | None:
        segments: list[str] = []
        for child in attrpath_node.named_children:
            if child.type != "identifier":
                return None
            segments.append(encoded[child.start_byte : child.end_byte].decode())
        return segments

    def search(node: Any, remaining: list[str]) -> Any | None:
        for binding in _direct_bindings(node):
            attrpath_node = binding.child_by_field_name("attrpath")
            if attrpath_node is None:
                continue
            segments = segments_of(attrpath_node)
            if segments is None:
                continue
            count = len(segments)
            if segments != remaining[:count]:
                continue
            if count == len(remaining):
                return binding
            value = binding.child_by_field_name("expression")
            if value is None:
                continue
            found = search(value, remaining[count:])
            if found is not None:
                return found
        return None

    found = search(tree.root_node, list(path))
    if found is None:
        return None
    return (found.start_point[0], found.start_point[1], found.end_point[0], found.end_point[1])


_NOQA_RE = re.compile(r"#\s*noqa:\s*(?P<code>[A-Z]+\d+)\s*--\s*(?P<reason>.+?)\s*$")


def suppressed_codes_on_line(source: str, line: int) -> set[str]:
    """Return every rule code suppressed by a ``# noqa: CODE -- reason`` comment on *line* (0-indexed).

    A plain per-line regex scan, not a tree-sitter comment-node walk: Nix's
    grammar attaches ``#``-comments as trivia to whatever token follows,
    the same node-adjacency fragility this module's docstring already flags
    for completion. Single code per comment in v1, matching this
    repository's own ``# noqa: RULE -- reason`` Python convention.
    """
    lines = source.splitlines()
    if not 0 <= line < len(lines):
        return set()
    match = _NOQA_RE.search(lines[line])
    return {match.group("code")} if match else set()
