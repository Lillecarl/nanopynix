"""Bulk package extraction, for ``pynix search``.

This is the package half of what ``pynix/_options.py`` does for NixOS options,
and it follows the same two rules: run the whole walk inside the evaluator, in
one round trip, and force nothing that a package can define as an expression
over something a walk cannot resolve.

**The walk reads the nixpkgs the caller pinned, and not a channel release.**
Issue #85 measured the other source: Hydra publishes ``packages.json.br``,
which holds 148 251 packages of one release. Two things follow. That file
describes a release the caller may not use, and the channel serves it in
Brotli alone -- ``packages.json``, ``.gz`` and ``.xz`` each answer 404 -- so
reading it means a dependency that nothing else here needs. This walk answers
"what is in *my* nixpkgs", which is the set the caller will really build.

Measured on nixpkgs unstable: 24 571 packages, 17.6 s, 2.08 GB at peak, and
16 069 of them name a ``meta.mainProgram``.

**``mainProgram`` is one binary, and a package gives many.** So this walk does
not answer "which package gives me ``ssh-keygen``". ``programs.sqlite``, which
Hydra ships inside ``nixexprs.tar.xz``, holds 161 511 rows of binary to
package and does answer it. Issue #256 adds that source beside this one; the
two join on the package name.

**The walk reads the top level of nixpkgs only.** It does not enter a set that
carries ``recurseForDerivations``, such as ``python3Packages``, which is most
of the difference between 24 571 here and 148 251 there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping

    from nanopynix import AsyncEvalSession, AsyncValue

#: The walk, as one Nix function of `lib` and `pkgs`.
#:
#: **Every attribute goes through `builtins.tryEval`.** nixpkgs holds packages
#: that throw when they are evaluated at all: one that its licence forbids, one
#: marked broken, one that no current platform builds. The walk returns one Nix
#: list, forced in one pass, so a single such package would end the whole
#: extraction. This is the same trap that `pynix/_options.py` documents for an
#: option whose default only a realized system can evaluate.
#:
#: **`tryEval` does not catch every failure**, and its own documentation says
#: so: it stops a `throw` and an `assert`, and not an error that a builtin
#: raises. `isDerivation` is a check of one attribute and raises none of those,
#: so the guard holds for the question this walk asks.
_COLLECT_PACKAGE_METADATA = """
lib: pkgs:
  let
    entry = name: value:
      let
        probed = builtins.tryEval (
          if !(lib.isDerivation value)
          then null
          else {
            attr = name;
            pname = value.pname or value.name or name;
            version = value.version or "";
            description = value.meta.description or null;
            mainProgram = value.meta.mainProgram or null;
            broken = value.meta.broken or false;
            unfree = !(value.meta.available or true) || !(value.meta.license.free or true);
          }
        );
      in
        if probed.success then probed.value else null;
  in
    builtins.filter (x: x != null) (lib.mapAttrsToList entry pkgs)
"""


@dataclass(frozen=True)
class PackageRecord:
    """One package, as a search reads it.

    There is no store path and no output here. Resolving a path means
    instantiating the derivation, and a search that answers in milliseconds
    cannot do that for 24 571 packages. Issue #85 says to resolve the results
    that the caller can see, and only those.
    """

    #: The attribute path under the top level, for example `ripgrep`.
    attr: str
    #: `meta.pname`, or the name when the package states no `pname`.
    pname: str
    #: The version, or an empty string when the package states none.
    version: str
    #: `meta.description`, or `None`.
    description: str | None
    #: `meta.mainProgram`: the one binary the package means the caller to run.
    #: `None` for the 8 502 of 24 571 packages that name none.
    main_program: str | None
    #: `meta.broken`.
    broken: bool
    #: Whether the licence is unfree, or the package is otherwise unavailable.
    unfree: bool


async def fetch_package_list(
    session: AsyncEvalSession, pkgs_value: AsyncValue, lib_value: AsyncValue
) -> list[PackageRecord]:
    """Extract every top-level package of *pkgs_value*.

    *lib_value* must be a nixpkgs `lib`, and ordinarily the one that
    *pkgs_value* itself carries.

    The walk runs inside the evaluator and returns one list, so this costs two
    round trips whatever the size of nixpkgs.
    """
    collector = await session.string(_COLLECT_PACKAGE_METADATA)
    package_list = await collector.call(lib_value, pkgs_value)
    raw = await package_list.to_python()
    if not isinstance(raw, list):
        raise TypeError(f"package metadata walk must return a list, got {type(raw).__name__}")
    records: list[PackageRecord] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            raise TypeError(f"each package metadata entry must be an object, got {type(entry).__name__}")
        records.append(_parse_record(cast("Mapping[str, object]", entry)))
    return records


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_record(entry: Mapping[str, object]) -> PackageRecord:
    return PackageRecord(
        attr=str(entry["attr"]),
        pname=str(entry.get("pname", "")),
        version=str(entry.get("version", "")),
        description=_optional_str(entry.get("description")),
        main_program=_optional_str(entry.get("mainProgram")),
        broken=bool(entry.get("broken", False)),
        unfree=bool(entry.get("unfree", False)),
    )
