from __future__ import annotations

# pyright: reportUnknownMemberType=false
# nanopynix / nanopynix_proto are C++ nanobind extensions without type stubs.
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

import structlog
from clypi import Command, Positional, arg
from rich.console import Console
from rich.tree import Tree

import nanopynix
from nanopynix._typechecking import BEARTYPING
from pynix._util import eval_session, print_json

if TYPE_CHECKING or BEARTYPING:
    from nanopynix import AsyncValue

logger = structlog.get_logger(__name__)
console = Console()

_DEFAULT_STORE = "auto"
_MAX_TREE_NODES = 200
_MAX_TREE_DEPTH = 6
_MAX_LIST_PREVIEW_ITEMS = 10


@dataclass
class _TreeBudget:
    remaining: int = _MAX_TREE_NODES


class Show(Command):
    """Show the outputs provided by a flake"""

    flake_ref: Positional[str] = arg(help="Flake reference (e.g. '.#' or 'nixpkgs#').")
    attrpath: str | None = arg(
        None,
        short="A",
        help="Dot-separated attribute path within the flake outputs to start from.",
    )
    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        base_ref, _, flake_attr = self.flake_ref.partition("#")
        flake_attr = flake_attr or None

        async with eval_session(self.store) as (_nix, _store, session):
            outputs = await session.eval_flake(base_ref)
            if flake_attr:
                outputs = _navigate(outputs, flake_attr)
            if self.attrpath:
                outputs = _navigate(outputs, self.attrpath)

            tree = Tree(f"[bold]{self.flake_ref}[/bold]")
            await _build_tree(tree, outputs, nanopynix.NixType, budget=_TreeBudget())
            console.print(tree)


class Metadata(Command):
    """Show locked flake metadata"""

    flake_ref: Positional[str] = arg(help="Flake reference (e.g. '.' or 'nixpkgs').")
    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _print_flake_metadata(self.flake_ref, store_uri=self.store)


class Info(Command):
    """Alias for flake metadata"""

    flake_ref: Positional[str] = arg(help="Flake reference (e.g. '.' or 'nixpkgs').")
    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _print_flake_metadata(self.flake_ref, store_uri=self.store)


def _navigate(root: AsyncValue, attrpath: str) -> AsyncValue:
    for part in attrpath.split("."):
        root = root.attr(part)
    return root


async def _build_tree(  # noqa: C901 -- tracked complexity/arg-count debt, see TODO.md
    tree: Tree,
    value: AsyncValue,
    nix_type_enum: type[nanopynix.NixType],
    *,
    depth: int = 0,
    budget: _TreeBudget,
) -> None:
    if depth > _MAX_TREE_DEPTH:
        tree.add("[dim]<...>[/dim]")
        return
    try:
        nix_type = await value.get_type()
    except Exception:
        tree.add("[dim]<unresolved>[/dim]")
        return
    if nix_type == nix_type_enum.ATTRS:
        names: list[str] = []
        with contextlib.suppress(Exception):
            names = await value.attr_names()
        if "type" in names and "drvPath" in names:
            tree.add("[dim]<derivation>[/dim]")
            return
        for name in names:
            if budget.remaining == 0:
                tree.add("[dim]<...>[/dim]")
                break
            budget.remaining -= 1
            child = value.attr(name)
            child_type = nix_type_enum.UNSPECIFIED
            with contextlib.suppress(Exception):
                child_type = await child.get_type()
            label = _format_attr(name, child_type, nix_type_enum)
            branch = tree.add(label)
            await _build_tree(branch, child, nix_type_enum, depth=depth + 1, budget=budget)
    elif nix_type == nix_type_enum.LIST:
        length = 0
        with contextlib.suppress(Exception):
            length = await value.list_length()
        for i in range(min(length, _MAX_LIST_PREVIEW_ITEMS)):
            if budget.remaining == 0:
                tree.add("[dim]<...>[/dim]")
                break
            budget.remaining -= 1
            child = value.list_get(i)
            child_type = nix_type_enum.UNSPECIFIED
            with contextlib.suppress(Exception):
                child_type = await child.get_type()
            label = _format_attr(f"[{i}]", child_type, nix_type_enum)
            branch = tree.add(label)
            await _build_tree(branch, child, nix_type_enum, depth=depth + 1, budget=budget)
        if length > _MAX_LIST_PREVIEW_ITEMS:
            tree.add(f"[dim]... {length - _MAX_LIST_PREVIEW_ITEMS} more items[/dim]")
    elif nix_type in (nix_type_enum.THUNK, nix_type_enum.UNSPECIFIED):
        tree.add(f"[dim]{nix_type.name.lower()}[/dim]")


def _format_attr(name: str, nix_type: nanopynix.NixType, nix_type_enum: type[nanopynix.NixType]) -> str:  # noqa: PLR0911 -- tracked complexity/arg-count debt, see TODO.md
    if nix_type == nix_type_enum.ATTRS:
        return f"[cyan]{name}[/cyan]"
    if nix_type == nix_type_enum.LIST:
        return f"[cyan]{name}[/cyan]"
    if nix_type == nix_type_enum.FUNCTION:
        return f"[yellow]{name}[/yellow]: [dim]<function>[/dim]"
    if nix_type == nix_type_enum.STRING:
        return f"[green]{name}[/green]: [dim]<string>[/dim]"
    if nix_type == nix_type_enum.INT:
        return f"[green]{name}[/green]: [dim]<int>[/dim]"
    if nix_type == nix_type_enum.FLOAT:
        return f"[green]{name}[/green]: [dim]<float>[/dim]"
    if nix_type == nix_type_enum.BOOL:
        return f"[green]{name}[/green]: [dim]<bool>[/dim]"
    if nix_type == nix_type_enum.NULL:
        return f"[green]{name}[/green]: [dim]<null>[/dim]"
    if nix_type == nix_type_enum.PATH:
        return f"[green]{name}[/green]: [dim]<path>[/dim]"
    return f"[green]{name}[/green]: [dim]<{nix_type.name.lower()}>[/dim]"


async def _print_flake_metadata(flake_ref: str, *, store_uri: str) -> None:
    """Print what ``nix flake metadata --json`` prints for *flake_ref*.

    Nix writes the whole object itself, in C++, so nothing here assembles it.
    That is what makes the two commands agree, ``locks`` included: the lock
    graph is a graph, and a flat map of the top level could never carry a
    transitive node or a ``follows`` edge.
    """
    async with eval_session(store_uri) as (_nix, _store, session):
        locked = await session.lock_flake(flake_ref, write_lock_file=False)
        try:
            print_json(json.loads(await locked.metadata_json()))
        finally:
            await locked.release()


class Flake(Command):
    """Inspect and manage Nix flakes"""

    subcommand: Show | Metadata | Info
