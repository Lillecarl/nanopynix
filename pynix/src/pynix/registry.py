from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._settings import ConfiguredCommand, PynixCommand, store_option

#: The help of ``--registry``, which every write takes.
#:
#: This is ``nix registry --registry``. An absent one means the registry of
#: the user, which is ``$XDG_CONFIG_HOME/nix/registry.json``. ``pynix registry
#: list`` reports that path, so a caller never has to derive it.
#:
#: A shared declaration would be a function, and a function that returns what
#: ``opt`` returns cannot state the type of the field it fills. So the three
#: commands share the text and each one declares its own option.
_REGISTRY_HELP = "Registry file to change. Defaults to the registry of the user."


class List(ConfiguredCommand):
    """List every flake registry entry, with the layer that holds it"""

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.registry.run_list(self)


class Add(ConfiguredCommand):
    """Point one flake reference at another"""

    from_ref: str = pos(help="Flake reference to resolve, for example 'nixpkgs'.")

    to_ref: str = pos(help="Flake reference it resolves to, for example 'github:NixOS/nixpkgs'.")

    registry: str | None = opt(None, help=_REGISTRY_HELP)

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.registry.run_add(self)


class Remove(ConfiguredCommand):
    """Drop every entry for one flake reference"""

    from_ref: str = pos(help="Flake reference to drop, for example 'nixpkgs'.")

    registry: str | None = opt(None, help=_REGISTRY_HELP)

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.registry.run_remove(self)


class Pin(ConfiguredCommand):
    """Pin a flake reference to the reference it resolves to now"""

    from_ref: str = pos(help="Flake reference to pin, for example 'nixpkgs'.")

    locked: str | None = pos(
        default=None,
        help="Flake reference to pin it to. Defaults to the reference itself.",
    )

    registry: str | None = opt(None, help=_REGISTRY_HELP)

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.registry.run_pin(self)


class Registry(PynixCommand):
    """Manage the flake registry, which resolves an indirect flake reference"""

    subcommands = (List, Add, Remove, Pin)
