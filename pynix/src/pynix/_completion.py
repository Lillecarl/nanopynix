"""Shared attribute-completion-prefix detection for pynix's REPL and language server.

Both consumers need to answer the same question -- "given source text and a
cursor position, what attribute path is being completed?" -- but had
independently evolved two different strategies with different strengths:
the REPL's tree-sitter walk can complete after an *arbitrary* base
expression (e.g. ``(import ./foo.nix).ba``), not just a plain identifier
chain, while the language server's plain lexical scan tolerates the
malformed, mid-edit text a tree-sitter parse struggles to recover from
cleanly. ``completion_prefix_at`` combines both: a structural (tree-sitter)
tier tried first, falling back to a lexical (regex) tier whenever the
structural tier finds nothing ending exactly at the cursor.
"""

from __future__ import annotations

import re
from typing import Any

from pynix._nix_syntax import parse_nix

_ATTRPATH_TAIL_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_'.-])((?:[A-Za-z_][A-Za-z0-9_'-]*\.)*)([A-Za-z_][A-Za-z0-9_'-]*)?$",
)


def _nodes(root: Any) -> list[Any]:
    """Return all nodes below *root* in source order."""
    nodes: list[Any] = [root]
    result: list[Any] = []
    while nodes:
        node = nodes.pop()
        result.append(node)
        nodes.extend(reversed(node.children))
    return result


class _NoMatch:
    """Sentinel type: tier 1 doesn't apply at all at this position, try tier 2."""


_NO_MATCH = _NoMatch()

_COMMENT_NODE_TYPES = frozenset({"line_comment", "block_comment", "doc_comment"})


def _inside_comment(nodes: list[Any], byte_offset: int) -> bool:
    """True if *byte_offset* falls inside a ``#``/``/* */`` comment span.

    Comments are tree-sitter ``extras`` -- real leaf nodes in the tree, just
    not referenced by any grammar rule -- so they show up in a plain node
    walk like anything else. Without this check, the lexical tier (which
    only looks at raw text before the cursor with no structural awareness)
    would happily match an identifier chain that only exists inside a
    comment, e.g. ``# see pkgs.lib.<cursor>`` offering completions for
    ``pkgs.lib`` as if it were real code.
    """
    return any(node.type in _COMMENT_NODE_TYPES and node.start_byte <= byte_offset <= node.end_byte for node in nodes)


def _attrpath_binding_prefix_before_dot(
    nodes: list[Any],
    byte_offset: int,
    encoded: bytes,
) -> tuple[str, str] | None:
    """If a binding's own attrpath ends right before the dot at *byte_offset*, return its prefix.

    E.g. for ``a.b.c.`` in ``a.b.c.d = 1;`` with the cursor right after that
    dot, returns ``("a.b.c", "")``. None if no attrpath identifier ends
    there, or its parent isn't a definition key (a ``select_expression``
    reference instead, e.g. ``cfg.services.foo.``, is handled by the
    generic nested-node search this is tried before).
    """
    for node in nodes:
        if node.type != "identifier" or node.end_byte != byte_offset - 1:
            continue
        attrpath = node.parent
        if attrpath is None or attrpath.type != "attrpath":
            continue
        binding = attrpath.parent
        if binding is None or binding.type != "binding":
            continue
        segments: list[str] = []
        for segment in attrpath.named_children:
            if segment.type != "identifier":
                continue
            segments.append(encoded[segment.start_byte : segment.end_byte].decode())
            if segment.end_byte == node.end_byte:
                break
        return ".".join(segments), ""
    return None


def _flat_identifier_chain_before_dot(nodes: list[Any], byte_offset: int, encoded: bytes) -> str | None:
    """Reconstruct a dotted identifier chain ending right before the dot at *byte_offset* by walking siblings.

    Covers the shape ``_attrpath_binding_prefix_before_dot`` can't: a
    *not-yet-assigned* definition-key attrpath (no ``=`` typed yet, e.g. the
    very moment a user finishes typing ``kubernetes.objects.default.
    Deployment.demo.`` and nothing follows). With no ``=``, tree-sitter-nix
    never forms an ``attrpath``/``binding`` node at all -- the whole chain
    (``identifier``, ``.``, ``identifier``, ``.``, ...) collapses into a
    single flat ``ERROR`` node's own direct children instead, which is why
    the "widest clean expression" search below only ever finds the lone
    trailing identifier (the only individually error-free leaf in that
    shape): it has no multi-segment node to find, since the multi-segment
    grouping only exists as the ERROR node itself.

    Since both shapes -- a clean ``attrpath``'s children and an ERROR node's
    flattened children -- are simple ``identifier``/``.`` siblings either
    way, one backward sibling-walk from the dot handles both (this doesn't
    replace ``_attrpath_binding_prefix_before_dot``, which is tried first and
    already correct there, but is a safe, strictly additive fallback).

    Correctly does *not* fire for a dangling dot after a reference chain
    (e.g. ``cfg.services.foo.`` used as a value): there, the chain itself
    (``cfg.services.foo``) parses clean as its own ``select_expression``,
    and the dangling ``.`` lands in its *own* isolated single-child ERROR
    node with no identifier sibling at all -- so the walk immediately finds
    nothing and returns None, leaving that case to the existing widest-node
    search below.
    """
    dot = next((node for node in nodes if node.type == "." and node.end_byte == byte_offset), None)
    if dot is None:
        return None
    segments: list[str] = []
    node = dot.prev_sibling
    while node is not None and node.type == "identifier":
        segments.append(encoded[node.start_byte : node.end_byte].decode())
        dot_node = node.prev_sibling
        if dot_node is None or dot_node.type != ".":
            break
        node = dot_node.prev_sibling
    if not segments:
        return None
    segments.reverse()
    return ".".join(segments)


def _tree_prefix_at(source: str, byte_offset: int) -> tuple[str | None, str] | None | _NoMatch:  # noqa: C901, PLR0911 tracked complexity/arg-count debt, see TODO.md
    """Tier 1: a tree-sitter structural match ending exactly at *byte_offset*.

    Matches three shapes: a ``select_expression`` (so it can complete after
    an arbitrary base expression, the one thing the lexical tier below
    cannot structurally represent), a bare string-interpolation open
    (``"${``, offering an unfiltered "complete from ambient scope" the same
    way a bare/no-prefix position would), and the one dangling-dot case a
    bare structural match can't reach directly -- a base expression
    immediately followed by ``.`` with nothing typed after it yet, found by
    looking for a clean (error-free) node ending right before that dot in
    the original parse tree. Everything else
    (bare names, plain dotted chains, and anything sitting on malformed/
    mid-edit text, e.g. a partially-typed identifier) is left to the lexical
    tier, which already handles all of those correctly and more simply.

    Returns a ``_NoMatch`` sentinel when tier 1 doesn't apply at all at this
    position (callers should fall back to the lexical tier), or ``None`` when tier 1
    positively recognizes the position as structurally unresolvable (e.g. a
    dynamic/interpolated attrpath segment like ``pkgs.${x}`` -- a completable
    ``select_expression`` ends here, but its last segment isn't a plain
    identifier) -- that ``None`` must propagate as "no completion" rather
    than be reinterpreted as "try the lexical tier", since a raw-text regex
    scan has no idea the position is inside a dynamic key and would offer an
    unrelated, nonsensical completion instead of correctly offering none.

    Also positively resolves (returns ``None``, not ``_NoMatch``) whenever
    *byte_offset* sits inside a comment -- see ``_inside_comment``.
    """
    encoded = source.encode()
    nodes = _nodes(parse_nix(source).root_node)

    if _inside_comment(nodes, byte_offset):
        return None

    def text(node: Any) -> str:
        return encoded[node.start_byte : node.end_byte].decode()

    selections = [node for node in nodes if node.type == "select_expression" and node.end_byte == byte_offset]
    if selections:
        target = selections[-1]
        attrpath = target.child_by_field_name("attrpath")
        if attrpath is None:
            return None
        attributes = [node for node in attrpath.named_children if node.type == "identifier"]
        if not attributes:
            return None
        partial_node = attributes[-1]
        prefix = encoded[target.start_byte : partial_node.start_byte].rstrip(b".").decode()
        return (prefix, text(partial_node)) if prefix else None

    interpolation_starts = [node for node in nodes if node.type == "${" and node.end_byte == byte_offset]
    if interpolation_starts:
        return None, ""

    if byte_offset > 0 and encoded[byte_offset - 1 : byte_offset] == b".":
        # An attrpath *definition key* (e.g. `a.b.c.` in `a.b.c.d = 1;`) is
        # checked first: unlike a reference chain (`a.b.c`, parsed as nested
        # select_expressions, each wrapping a wider prefix -- see the
        # "widest-spanning" comment below), a binding's own attrpath is a
        # single flat node with each segment as a direct, unnested sibling
        # identifier. So the generic widest-node search below would only
        # ever find the one identifier ending exactly at the dot (e.g. just
        # "c"), silently losing "a.b." -- this needs its own reconstruction,
        # walking the attrpath's siblings up to that point, mirroring
        # ``identifier_path_at``'s own binding-attrpath handling in
        # ``pynix/_lsp/_syntax.py``.
        attrpath_prefix = _attrpath_binding_prefix_before_dot(nodes, byte_offset, encoded)
        if attrpath_prefix is not None:
            return attrpath_prefix

        chain = _flat_identifier_chain_before_dot(nodes, byte_offset, encoded)
        if chain is not None:
            return chain, ""

        # Search the *original* source's own parse tree for a node ending
        # right before the dot -- NOT a separate re-parse of the text before
        # it in isolation. A real LSP document's "before the dot" text is an
        # arbitrary slice of a much larger file, not a standalone parseable
        # unit on its own (unlike the REPL's always-self-contained input
        # line), so re-parsing it in isolation spuriously introduces
        # unclosed-construct errors that don't exist in the real document.
        # Tree-sitter's error recovery already quarantines the dangling dot
        # itself into its own ERROR node without marking the clean
        # expression just before it, so the original tree already has
        # everything needed.
        expressions = [
            node for node in nodes if node.start_byte < node.end_byte == byte_offset - 1 and not node.has_error
        ]
        if expressions:
            # The widest-spanning match, not just the last one found in
            # traversal order -- a nested chain like "cfg.services" parses as
            # a select_expression wrapping its own last "services" identifier,
            # both ending at the same byte; picking by traversal order alone
            # would grab that inner identifier and lose the "cfg." part. No
            # type restriction here (unlike the ``selections`` check above):
            # any complete, error-free expression counts, e.g. a
            # parenthesized function application, so the dangling-dot case
            # supports arbitrary base expressions just like the typed-partial
            # case above it.
            target = min(expressions, key=lambda node: node.start_byte)
            prefix = text(target)
            return (prefix, "")

    return _NO_MATCH


def completion_prefix_at(source: str, byte_offset: int) -> tuple[str | None, str] | None:
    """Return ``(prefix_text, partial)`` for attribute completion at *byte_offset*.

    Two tiers, tried in order:

    1. A tree-sitter structural match (``_tree_prefix_at``), which can
       complete after an arbitrary base expression, not just an identifier
       chain -- but only fires when a node actually ends exactly at
       *byte_offset*, so it never engages on malformed mid-edit text.
    2. A lexical regex scan over the text immediately before the cursor,
       which handles everything else: plain identifier chains at any
       position, including mid-token (the common case while a user is
       actively typing a completion), and text tier 1 can't parse cleanly.

    ``prefix_text`` is ``None`` for a bare/no-prefix position (e.g. no ``.``
    typed at all yet, in which case a caller with an ambient lexical scope
    -- like the REPL -- should complete from that instead); otherwise it is
    raw Nix source text for the base expression -- a plain dotted-identifier
    chain from tier 2, or, reachable only via tier 1, an arbitrary
    expression. A caller that can only resolve plain identifier chains (like
    the language server, which walks named roots) is responsible for
    validating that shape itself before splitting the text on ``.``.
    """
    tree_result = _tree_prefix_at(source, byte_offset)
    if isinstance(tree_result, _NoMatch):
        text_before = source.encode()[:byte_offset].decode()
        match = _ATTRPATH_TAIL_RE.search(text_before)
        if match is None:
            return None
        prefix_dotted, partial = match.groups()
        prefix_segments = [segment for segment in (prefix_dotted or "").split(".") if segment]
        partial = partial or ""
        if not prefix_segments and not partial:
            return None
        return (".".join(prefix_segments) if prefix_segments else None), partial
    return tree_result
