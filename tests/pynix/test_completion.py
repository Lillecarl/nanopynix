"""Tests for the shared attribute-completion-prefix detector.

Covers both tiers of ``completion_prefix_at`` directly -- the tree-sitter
structural tier (which can complete after an arbitrary base expression, not
just an identifier chain) and the lexical regex fallback (which handles
mid-token positions and malformed/incomplete text) -- plus the specific
correctness cases discovered while unifying the REPL's and the language
server's previously-separate implementations.
"""

from __future__ import annotations

from pynix._completion import completion_prefix_at


def test_completes_a_plain_dotted_chain_via_the_tree_tier() -> None:
    source = "cfg.serv"
    assert completion_prefix_at(source, len(source)) == ("cfg", "serv")


def test_completes_a_bare_identifier_with_no_prefix() -> None:
    source = "fo"
    assert completion_prefix_at(source, len(source)) == (None, "fo")


def test_completes_after_an_arbitrary_expression_prefix() -> None:
    """The one thing only the tree-sitter tier can do -- the regex tier
    structurally cannot represent a non-identifier base expression."""
    source = "(import ./foo.nix).ba"
    assert completion_prefix_at(source, len(source)) == ("(import ./foo.nix)", "ba")


def test_handles_a_dangling_dot_after_a_nested_chain() -> None:
    """Regression test: the widest-spanning tree node must be picked, not
    just the last one found in traversal order -- a naive pick grabbed the
    inner "services" identifier and lost the "cfg." part."""
    source = "cfg.services."
    assert completion_prefix_at(source, len(source)) == ("cfg.services", "")


def test_handles_a_dangling_dot_after_a_bare_name() -> None:
    source = "cfg."
    assert completion_prefix_at(source, len(source)) == ("cfg", "")


def test_handles_a_dangling_dot_after_an_arbitrary_expression() -> None:
    source = "(import ./foo.nix)."
    assert completion_prefix_at(source, len(source)) == ("(import ./foo.nix)", "")


def test_handles_a_dangling_dot_on_a_not_yet_assigned_definition_key() -> None:
    """Regression test: a definition-key attrpath with no `=` yet (the exact
    moment a user finishes typing a brand new binding's dotted path and
    hasn't typed anything else) loses everything but the trailing segment.

    With no `=`, tree-sitter-nix never forms an `attrpath`/`binding` node at
    all here -- the whole chain collapses into one flat `ERROR` node's own
    direct `identifier`/`.` children, unlike an already-complete binding
    (`a.b.c = 1;`, where a clean `attrpath` node exists) or a reference
    chain (`cfg.services.foo.` as a value, where the dangling dot lands in
    its own isolated single-child ERROR node and the chain itself still
    parses as a clean `select_expression`). The old "widest clean node
    ending right before the dot" search only ever found the lone trailing
    identifier in this shape, since the multi-segment grouping only exists
    as the (necessarily erroring) ERROR node itself -- silently truncating
    completion to just the last-typed segment the instant `.` is typed,
    until a further character is typed and the lexical regex tier (which
    doesn't care about tree structure at all) takes back over.
    """
    source = "{ x = 1; a.b.c. }"
    idx = source.index("c.") + len("c.")
    assert completion_prefix_at(source, idx) == ("a.b.c", "")


def test_completes_from_a_string_interpolation_open() -> None:
    source = '"${'
    assert completion_prefix_at(source, len(source)) == (None, "")


def test_returns_none_for_a_dynamic_attrpath_segment() -> None:
    """A completable select_expression ends here, but its last segment is a
    dynamic interpolation, not a plain identifier -- must not fall through
    to the regex tier, which would otherwise offer an unrelated completion."""
    source = "pkgs.${x}"
    assert completion_prefix_at(source, len(source)) is None


def test_returns_none_with_nothing_completable_at_all() -> None:
    assert completion_prefix_at(".", 1) is None


def test_returns_none_after_trailing_whitespace_with_no_partial() -> None:
    source = "1 + 1 "
    assert completion_prefix_at(source, len(source)) is None


def test_returns_none_for_a_dotted_chain_inside_a_line_comment() -> None:
    """Regression test: comments are tree-sitter extras, invisible to the
    lexical regex tier's raw-text scan -- without a structural check, typing
    inside a comment like ``# see pkgs.lib.`` would offer completions for
    ``pkgs.lib`` as if it were real, live code."""
    source = "# see pkgs.lib.foo\nx = 1"
    byte_offset = len("# see pkgs.lib.")
    assert completion_prefix_at(source, byte_offset) is None


def test_returns_none_for_a_dotted_chain_inside_a_block_comment() -> None:
    source = "/* pkgs.lib. */"
    byte_offset = len("/* pkgs.lib.")
    assert completion_prefix_at(source, byte_offset) is None


def test_falls_back_to_the_regex_tier_mid_token_inside_a_binding_attrpath() -> None:
    """A binding's own attrpath (a NixOS module's ``services.foo.enable =
    true;``) is not a select_expression at all, so the tree tier never
    engages here regardless of cursor position -- this must resolve via the
    lexical tier, matching the language server's completion semantics for
    definition-key completion."""
    source = "services.example-daemon.enable = true;"
    byte_offset = len("services.exam")
    assert completion_prefix_at(source, byte_offset) == ("services", "exam")
