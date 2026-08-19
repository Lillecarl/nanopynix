"""Direct unit tests for pynix.flake's private rendering helpers.

test_flake_show.py only exercises these through real flake evaluation, which
never produces Nix INT/FLOAT/BOOL/NULL attrs, list values, or unresolved
thunks at the top level of the test fixture flake. These "dumb coverage"
tests pin those branches down directly with small fakes instead of contriving
a real flake to expose them.
"""

# ruff: noqa: ASYNC109
# The doubles below subclass real ValueProxy/EvalSession/ReplSession
# classes, whose async methods take a `timeout` keyword; an override has
# to keep it. Same exemption, same reason as
# nanopynix/rpc/client/_session.py, where the real signatures live.

from __future__ import annotations

from nanopynix_proto.nix.common import NixType
from rich.tree import Tree

from nanopynix.rpc import ValueProxy
from pynix._impl.flake import (  # pyright: ignore[reportPrivateUsage] -- test unit-tests the private rendering helpers directly, see module docstring
    _build_tree,
    _format_attr,
    _TreeBudget,
)


class _FakeFlakeValue(ValueProxy):
    """The subset of ValueProxy that _build_tree relies on."""

    def __init__(
        self,
        nix_type: NixType,
        *,
        attrs: dict[str, _FakeFlakeValue] | None = None,
        items: list[_FakeFlakeValue] | None = None,
        raise_on_get_type: bool = False,
    ) -> None:
        self._type = nix_type
        self._attrs = attrs or {}
        self._items = items or []
        self._raise_on_get_type = raise_on_get_type

    # Each method takes and ignores `timeout` so its signature matches
    # ValueProxy's: this double subclasses the real class (so beartype's
    # isinstance checks accept it), and a subclass that drops an argument its
    # caller passes is not standing in for anything.
    async def get_type(self, *, timeout: float | None = None) -> NixType:
        if self._raise_on_get_type:
            raise RuntimeError("evaluation failed")
        return self._type

    async def attr_names(self, *, timeout: float | None = None) -> list[str]:
        return list(self._attrs)

    def attr(self, name: str, *, timeout: float | None = None) -> _FakeFlakeValue:
        return self._attrs[name]

    async def list_length(self, *, timeout: float | None = None) -> int:
        return len(self._items)

    def list_get(self, index: int, *, timeout: float | None = None) -> _FakeFlakeValue:
        return self._items[index]


async def test_build_tree_marks_unresolved_when_get_type_raises() -> None:
    tree = Tree("root")

    await _build_tree(
        tree,
        _FakeFlakeValue(NixType.THUNK, raise_on_get_type=True),
        NixType,
        budget=_TreeBudget(),
    )

    assert any("<unresolved>" in str(child.label) for child in tree.children)


async def test_build_tree_renders_list_values_and_truncates_past_ten() -> None:
    items = [_FakeFlakeValue(NixType.STRING) for _ in range(12)]
    value = _FakeFlakeValue(NixType.LIST, items=items)
    tree = Tree("root")

    await _build_tree(tree, value, NixType, budget=_TreeBudget())

    labels = [str(child.label) for child in tree.children]
    assert any("[0]" in label for label in labels)
    assert any("more items" in label for label in labels)


async def test_build_tree_renders_a_short_list_without_a_more_items_message() -> None:
    items = [_FakeFlakeValue(NixType.STRING) for _ in range(3)]
    value = _FakeFlakeValue(NixType.LIST, items=items)
    tree = Tree("root")

    await _build_tree(tree, value, NixType, budget=_TreeBudget())

    labels = [str(child.label) for child in tree.children]
    assert len(labels) == 3
    assert not any("more items" in label for label in labels)


async def test_build_tree_stops_list_rendering_once_the_budget_is_exhausted() -> None:
    items = [_FakeFlakeValue(NixType.STRING) for _ in range(3)]
    value = _FakeFlakeValue(NixType.LIST, items=items)
    tree = Tree("root")

    await _build_tree(tree, value, NixType, budget=_TreeBudget(remaining=0))

    labels = [str(child.label) for child in tree.children]
    assert labels == ["[dim]<...>[/dim]"]


async def test_build_tree_renders_thunk_as_a_dim_leaf() -> None:
    value = _FakeFlakeValue(NixType.THUNK)
    tree = Tree("root")

    await _build_tree(tree, value, NixType, budget=_TreeBudget())

    assert any("thunk" in str(child.label) for child in tree.children)


def test_format_attr_covers_the_remaining_scalar_types() -> None:
    assert "<int>" in _format_attr("n", NixType.INT, NixType)
    assert "<float>" in _format_attr("n", NixType.FLOAT, NixType)
    assert "<bool>" in _format_attr("n", NixType.BOOL, NixType)
    assert "<null>" in _format_attr("n", NixType.NULL, NixType)
