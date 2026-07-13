from __future__ import annotations

import contextlib
import json
import sys
from typing import TYPE_CHECKING, Any, override

import structlog
from clypi import Command, Positional, arg
from rich.console import Console
from rich.tree import Tree

from pynix._util import forward_nix_logs, prepare_sys_path

if TYPE_CHECKING:
    from nanopynix._session import ValueProxy

    from nanopynix_proto.nix.common import NixType

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
            forward_nix_logs(nix),
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


def _navigate(root: ValueProxy, attrpath: str) -> ValueProxy:
    for part in attrpath.split("."):
        root = root.attr(part)
    return root


async def _build_tree(tree: Tree, value: ValueProxy, nix_type_enum: type[NixType], *, depth: int = 0) -> None:
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


def _format_attr(name: str, nix_type: int, nix_type_enum: type[NixType]) -> str:
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
    prepare_sys_path()
    import nanopynix

    async with (
        nanopynix.Session(experimental_features=["flakes", "nix-command"]) as nix,
        forward_nix_logs(nix),
        nix.store(store_uri) as store,
        nix.eval(store) as session,
    ):
        locked = await session.lock_flake(flake_ref, write_lock_file=False)
        try:
            _print_json(_locked_flake_to_json(flake_ref, locked))
        finally:
            await locked.release()


def _locked_flake_to_json(flake_ref: str, locked: Any) -> dict[str, Any]:
    return {
        "resolvedRef": flake_ref,
        "description": locked.description,
        "inputs": {
            name: _locked_input_to_json(input_)
            for name, input_ in sorted(locked.inputs.items(), key=lambda item: item[0])
        },
    }


def _locked_input_to_json(input_: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "isFlake": input_.is_flake,
        "follows": list(input_.follows),
    }
    attrs = input_.attrs
    if attrs is not None:
        result["attrs"] = {
            name: _attrs_value_to_json(value) for name, value in sorted(attrs.entries.items(), key=lambda item: item[0])
        }
    return result


def _attrs_value_to_json(value: Any) -> str | int | bool | None:
    if value.bool_value is not None:
        return value.bool_value
    if value.int_value is not None:
        return value.int_value
    if value.string_value is not None:
        return value.string_value
    return None


def _print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")


class Flake(Command):
    """Inspect and manage Nix flakes"""

    subcommand: Show | Metadata | Info
