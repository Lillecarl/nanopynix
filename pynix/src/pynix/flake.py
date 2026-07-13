from __future__ import annotations

import contextlib
from typing import override

import structlog
from clypi import Command, Positional, arg
from rich.console import Console
from rich.tree import Tree

from pynix._util import prepare_sys_path

logger = structlog.get_logger(__name__)
console = Console()

_DEFAULT_STORE = "auto"


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
        prepare_sys_path()
        import nanopynix
        from nanopynix import NixType

        base_ref, _, flake_attr = self.flake_ref.partition("#")
        flake_attr = flake_attr or None

        async with (
            nanopynix.Session(experimental_features=["flakes", "nix-command"]) as nix,
            nix.store(self.store) as store,
            nix.eval(store) as session,
        ):
            outputs = await session.eval_flake(base_ref)
            if flake_attr:
                outputs = _navigate(outputs, flake_attr)
            if self.attrpath:
                outputs = _navigate(outputs, self.attrpath)

            tree = Tree(f"[bold]{self.flake_ref}[/bold]")
            await _build_tree(tree, outputs, NixType)
            console.print(tree)


def _navigate(root, attrpath: str):
    for part in attrpath.split("."):
        root = root.attr(part)
    return root


async def _build_tree(tree: Tree, value, nix_type_enum, *, depth: int = 0) -> None:
    if depth > 6:
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
        for name in names:
            child = value.attr(name)
            child_type = nix_type_enum.UNSPECIFIED
            with contextlib.suppress(Exception):
                child_type = await child.get_type()
            label = _format_attr(name, child_type, nix_type_enum)
            branch = tree.add(label)
            await _build_tree(branch, child, nix_type_enum, depth=depth + 1)
    elif nix_type == nix_type_enum.LIST:
        length = 0
        with contextlib.suppress(Exception):
            length = await value.list_length()
        for i in range(min(length, 10)):
            child = value.list_get(i)
            child_type = nix_type_enum.UNSPECIFIED
            with contextlib.suppress(Exception):
                child_type = await child.get_type()
            label = _format_attr(f"[{i}]", child_type, nix_type_enum)
            branch = tree.add(label)
            await _build_tree(branch, child, nix_type_enum, depth=depth + 1)
        if length > 10:
            tree.add(f"[dim]... {length - 10} more items[/dim]")
    elif nix_type in (nix_type_enum.THUNK, nix_type_enum.UNSPECIFIED):
        tree.add(f"[dim]{nix_type.name.lower()}[/dim]")


def _format_attr(name: str, nix_type, nix_type_enum) -> str:
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


class Flake(Command):
    """Inspect and manage Nix flakes"""

    subcommand: Show
