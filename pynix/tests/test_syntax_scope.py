"""Tests for `local_scope_at`/`path_literal_at` -- the syntax-only engine behind
go to definition, rename, document highlight, and find references.

Shadowing is easy to get subtly wrong (a naive "every reference with this
name" scan would happily rewrite an unrelated inner binding that just
happens to share a name), so it gets dedicated unit tests here, isolated
from the LSP wire protocol entirely -- mirroring how test_completion.py
unit-tests `_completion.py` in isolation.
"""

from __future__ import annotations

from pynix._lsp._syntax import local_scope_at, path_literal_at


def test_let_binding_definition_finds_every_reference() -> None:
    source = "let x = 1; y = x + 1; z = x + 2; in y + z"
    scope = local_scope_at(source, source.index("x ="))
    assert scope is not None
    assert source[scope.definition[1] : scope.definition[3]] == "x"
    reference_texts = {source[start:end] for _, start, _, end in scope.references}
    assert reference_texts == {"x"}
    assert len(scope.references) == 2


def test_let_binding_reference_resolves_back_to_the_same_scope_as_its_definition() -> None:
    source = "let x = 1; y = x + 1; z = x + 2; in y + z"
    from_definition = local_scope_at(source, source.index("x ="))
    from_reference = local_scope_at(source, source.rindex("x + 2"))
    assert from_definition == from_reference


def test_a_nested_let_binding_with_the_same_name_shadows_the_outer_one() -> None:
    """The inner `let x = 2; in x` must not be touched when renaming the outer `x`."""
    source = "let x = 1; inner = let x = 2; in x; outer = x; in inner + outer"
    scope = local_scope_at(source, source.index("x ="))
    assert scope is not None
    reference_texts = [source[start:end] for _, start, _, end in scope.references]
    # Only the final `outer = x;` reference belongs to the outer `x` -- the
    # `inner = let x = 2; in x;` subtree redeclares `x` and must be skipped
    # entirely, including its own `in x` reference.
    assert reference_texts == ["x"]
    outer_x_offset = source.rindex("x;")
    assert scope.references[0] == (0, outer_x_offset, 0, outer_x_offset + 1)


def test_a_formals_default_expression_sees_a_sibling_formal() -> None:
    """`{ a, b ? a + 1 }: a + b` -- `b`'s default is in `a`'s scope, not just the body."""
    source = "{ a, b ? a + 1 }: a + b"
    scope = local_scope_at(source, source.index("a,"))
    assert scope is not None
    reference_texts = [source[start:end] for _, start, _, end in scope.references]
    assert reference_texts == ["a", "a"]


def test_renaming_a_formal_from_a_reference_site_finds_the_same_scope_as_from_its_definition() -> None:
    source = "{ a, b ? a + 1 }: a + b"
    from_definition = local_scope_at(source, source.index("a,"))
    from_body_reference = local_scope_at(source, source.rindex("a + b"))
    assert from_definition == from_body_reference


def test_a_universal_at_pattern_binding_is_renameable() -> None:
    source = "args@{ a, ... }: args.a"
    scope = local_scope_at(source, source.index("args@") + len("args"))
    assert scope is not None
    reference_texts = [source[start:end] for _, start, _, end in scope.references]
    assert reference_texts == ["args"]


def test_an_inherited_name_is_not_renameable_and_does_not_fall_through_to_an_outer_scope() -> None:
    """`inherit`-bound names are shadowing-relevant but out of v1's renameable scope entirely."""
    source = "let x = 1; in let inherit x; in x"
    inherited_reference_offset = source.rindex("x")
    assert local_scope_at(source, inherited_reference_offset) is None


def test_a_multi_segment_attrpath_binding_is_not_renameable() -> None:
    source = "let a.b = 1; in a.b"
    assert local_scope_at(source, source.index("a.b")) is None


def test_a_module_system_option_path_has_no_local_scope() -> None:
    source = "{ config, ... }: { config.services.foo.enable = true; }"
    assert local_scope_at(source, source.index("enable")) is None


def test_path_literal_at_returns_the_raw_source_text() -> None:
    source = "import ./foo.nix"
    assert path_literal_at(source, source.index("./foo.nix") + 2) == "./foo.nix"


def test_path_literal_at_returns_none_for_an_interpolated_path() -> None:
    source = 'let x = "bar"; in import ./foo-${x}.nix'
    assert path_literal_at(source, source.rindex(".nix")) is None


def test_path_literal_at_returns_none_outside_any_path_literal() -> None:
    source = "let x = 1; in x"
    assert path_literal_at(source, source.index("x")) is None
