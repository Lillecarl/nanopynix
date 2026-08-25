"""Find the `options`, the `pkgs` and the `lib` of one evaluation target.

`pynix search` reads two indexes from one target. Option search needs an
options tree and a `lib`. Package search needs a package set, because the
binary-name database sits at `${pkgs.path}/programs.sqlite` and the package
walk reads the set itself. A person names one target, so this module answers
where each of the three lives.

**A module system does not always give `pkgs` back.** `specialArgs` reaches a
module and never reaches the result. Measured on nixpkgs 26.11::

    lib.evalModules { specialArgs.pkgs = ...; }   ->  _module.args = [ extendModules moduleType ]
    lib.evalModules { _module.args.pkgs = ...; }  ->  _module.args = [ extendModules moduleType pkgs ]

So a chain of candidates cannot cover every shape, and *pkgs_attr* is the
answer for a target that hides its package set. It is not a convenience.

**`_module` sits beside `config`, and not under it.** `evalModules` removes
`_module` from `config` and re-exports it at the top of the result, so the
path is `_module.args.pkgs`. `config._module.args.pkgs` resolves nowhere.

**A NixOS evaluation needs no override.** `nixos/lib/eval-config.nix` returns
`_module`, `config`, `lib`, `options` and `pkgs` together, and its `pkgs`
equals its `_module.args.pkgs`. The first link of each chain answers it.

**A missing value is not a failure.** A target that is a bare package set has
no options tree, and a module system with no reachable `pkgs` has no packages.
Each one still answers the search it can answer, so this module reports
`None` rather than raising. An explicit override that resolves nowhere does
raise, because a person who names a path asked for that path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanopynix_helpers import AttrPathNotFoundError

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixTypeError
from pynix.target import select_attr

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from nanopynix import AsyncValue

#: The attribute path that the automatic search tries for the options tree.
OPTIONS_CHAIN: tuple[str, ...] = ("options",)

#: The attribute path that the automatic search tries for the values of every
#: option. `options` declares an option and `config` holds what it came to, and
#: `eval-config.nix` returns the two beside each other.
#:
#: **No override goes with it, where the other three have one.** A `pkgs` that
#: `specialArgs` hid is the reason `--pkgs-attr` exists, and no such shape is
#: known for `config`: a module system that returns `options` returns `config`
#: beside it. An override with no caller is surface to keep and to test.
CONFIG_CHAIN: tuple[str, ...] = ("config",)

#: The attribute paths that the automatic search tries for the package set,
#: in order. A target that is itself a package set answers before them.
PKGS_CHAIN: tuple[str, ...] = ("pkgs", "_module.args.pkgs")

#: The attribute paths that the automatic search tries for `lib`. The `lib` of
#: the resolved package set follows them.
LIB_CHAIN: tuple[str, ...] = ("lib",)

#: What :attr:`Resolved.path` holds when the target itself is the value.
TARGET_PATH = "."

#: The two attributes that say a value is a package set rather than a module
#: system. `path` is the source of nixpkgs, and `stdenv` is its build
#: environment. A module system result carries neither.
_PACKAGE_SET_MARKERS: tuple[str, ...] = ("path", "stdenv")


@dataclass(frozen=True)
class Resolved:
    """One value of a search target, and where it came from."""

    #: The value itself.
    value: AsyncValue

    #: The attribute path, relative to the target, that gave the value.
    #: :data:`TARGET_PATH` means the target itself.
    path: str


@dataclass(frozen=True)
class SearchTarget:
    """What one evaluation target offers to a search.

    Any of the four is `None` when the target does not hold it.
    """

    options: Resolved | None
    pkgs: Resolved | None
    lib: Resolved | None

    #: The values of every option, which is what a reader sees in the pane
    #: beside the default. A target that declares options always has it, and a
    #: bare package set has neither.
    config: Resolved | None = None


async def _select(value: AsyncValue, path: str) -> Resolved | None:
    """*path* of *value*, or `None` when it resolves nowhere."""
    try:
        return Resolved(await select_attr(value, path), path)
    except AttrPathNotFoundError:
        return None


async def _first(value: AsyncValue, chain: Sequence[str]) -> Resolved | None:
    """The first path of *chain* that resolves against *value*."""
    for path in chain:
        found = await _select(value, path)
        if found is not None:
            return found
    return None


async def _is_package_set(value: AsyncValue) -> bool:
    """Say whether *value* looks like a package set.

    The test reads two attributes and forces neither, so it costs nothing on a
    value that is not one. A value that is not an attribute set answers `False`
    rather than raising, which is what `nix` itself does when it tries a
    candidate attribute path.
    """
    for marker in _PACKAGE_SET_MARKERS:
        try:
            present = await value.has_attr(marker)
        except NixTypeError:
            return False
        if not present:
            return False
    return True


async def resolve(
    target: AsyncValue,
    *,
    options_attr: str | None = None,
    pkgs_attr: str | None = None,
    lib_attr: str | None = None,
) -> SearchTarget:
    """Find the options tree, the package set, the `lib` and the `config` of *target*.

    **`config` is never forced.** It is the whole evaluated system, and
    forcing it here would charge every search for a field that a reader may
    never open. The pane forces the one path it draws.

    Each argument overrides the automatic search for one value. An override
    that resolves nowhere raises
    :class:`~nanopynix_helpers.eval_target.AttrPathNotFoundError`, and the
    automatic search reports `None` instead.
    """
    options = await _resolve_options(target, options_attr)
    pkgs = await _resolve_pkgs(target, pkgs_attr)
    lib = await _resolve_lib(target, pkgs, lib_attr)
    config = await _first(target, CONFIG_CHAIN)
    return SearchTarget(options=options, pkgs=pkgs, lib=lib, config=config)


async def _resolve_options(target: AsyncValue, options_attr: str | None) -> Resolved | None:
    """The options tree of *target*, by override or by the chain."""
    if options_attr is not None:
        return Resolved(await select_attr(target, options_attr), options_attr)
    return await _first(target, OPTIONS_CHAIN)


async def _resolve_pkgs(target: AsyncValue, pkgs_attr: str | None) -> Resolved | None:
    """The package set of *target*, by override or by the chain."""
    if pkgs_attr is not None:
        return Resolved(await select_attr(target, pkgs_attr), pkgs_attr)
    # The target itself comes first. Real nixpkgs holds a `pkgs` attribute of
    # its own, which is the self-reference that cross-compilation splices, so
    # the chain would answer here and name the wrong path. A module system
    # result carries neither marker, so the chain still answers for one.
    if await _is_package_set(target):
        return Resolved(target, TARGET_PATH)
    return await _first(target, PKGS_CHAIN)


async def _resolve_lib(target: AsyncValue, pkgs: Resolved | None, lib_attr: str | None) -> Resolved | None:
    """The `lib` of *target*, by override, by the chain, or from the package set."""
    if lib_attr is not None:
        return Resolved(await select_attr(target, lib_attr), lib_attr)
    found = await _first(target, LIB_CHAIN)
    if found is not None:
        return found
    if pkgs is None:
        return None
    from_pkgs = await _select(pkgs.value, "lib")
    if from_pkgs is None:
        return None
    prefix = "" if pkgs.path == TARGET_PATH else f"{pkgs.path}."
    return Resolved(from_pkgs.value, f"{prefix}lib")
