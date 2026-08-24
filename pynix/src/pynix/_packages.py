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

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping, Sequence

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


def _record_fields(record: PackageRecord) -> dict[str, object]:
    """One record, in the shape the walk itself produces.

    **Not `dataclasses.asdict`.** That writes the field names of the class, and
    `_parse_record` reads the names the Nix expression uses: `mainProgram`
    against `main_program`. The two disagreed, so every cached package lost its
    main program and `rg` stopped finding `ripgrep` from a warm cache. Writing
    the walk's own shape keeps one parser for both paths, which is the only
    thing that makes them agree by construction.
    """
    return {
        "attr": record.attr,
        "pname": record.pname,
        "version": record.version,
        "description": record.description,
        "mainProgram": record.main_program,
        "broken": record.broken,
        "unfree": record.unfree,
    }


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


#: Bump this when `PackageRecord` gains or loses a field. A cache written by an
#: older `pynix` is then ignored rather than read into the wrong shape.
_CACHE_VERSION = 1


def cache_path(identity: str) -> Path:
    """Where the walk of the nixpkgs named by *identity* is kept.

    **The identity is a store path, so the cache needs no expiry.** `pkgs.path`
    is the source of nixpkgs in the store, and a store path is the hash of what
    is under it. Evaluation is pure, so the same path gives the same walk for
    ever: a hit is exactly right, and a different pin simply has a different
    file. `search` keys its own cache by the target it was given, and has to
    trust that; this one cannot be stale.
    """
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    directory = cache_home / "pynix" / "packages"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{Path(identity).name}.json"


def _cached_entries(path: Path) -> list[object] | None:
    """The package entries in the cache at *path*, or `None` for anything else.

    A cache is a convenience, so a file that is missing, truncated or written
    by another version is not an error: the caller walks nixpkgs again.
    """
    if not path.is_file():
        return None
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    fields = cast("dict[str, object]", payload)
    if fields.get("version") != _CACHE_VERSION:
        return None
    packages = fields.get("packages")
    return cast("list[object]", packages) if isinstance(packages, list) else None


def load_cache(path: Path) -> list[PackageRecord] | None:
    """Read a cached walk, or `None` when there is none this version can read.

    The entries go through `_parse_record`, the same function the walk itself
    uses, so a cache and a fresh walk cannot disagree about the shape.
    """
    entries = _cached_entries(path)
    if entries is None:
        return None
    records: list[PackageRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        records.append(_parse_record(cast("Mapping[str, object]", entry)))
    return records


def save_cache(path: Path, identity: str, records: Sequence[PackageRecord]) -> None:
    """Write the walk to *path*, whole or not at all.

    The write goes to a neighbouring file and is then renamed, because two
    `pynix` processes can index at once and a reader must never see half a
    file. A rename within one directory is atomic.
    """
    payload = {
        "version": _CACHE_VERSION,
        "identity": identity,
        "packages": [_record_fields(record) for record in records],
    }
    partial = path.with_suffix(f".{os.getpid()}.partial")
    partial.write_text(json.dumps(payload))
    partial.replace(path)


async def package_identity(pkgs_value: AsyncValue) -> str:
    """The store path of the nixpkgs that *pkgs_value* came from.

    `pkgs.path` is what nixpkgs itself calls its own source.

    **Two package sets from one source share this key, and issue #260 holds
    the measurement.** `import <nixpkgs> { }` and `import <nixpkgs> {
    config.allowUnfree = true; }` have the same `path`, so the second reads
    the walk of the first. `pkgs.config` cannot join the key as it stands,
    because a real config holds functions and `builtins.toJSON` raises on one
    where `builtins.tryEval` does not catch it.
    """
    path_value = pkgs_value.attr("path")
    return str(await path_value.to_python())


async def indexed_packages(
    session: AsyncEvalSession,
    pkgs_value: AsyncValue,
    lib_value: AsyncValue,
    *,
    refresh: bool = False,
) -> list[PackageRecord]:
    """Return the packages of *pkgs_value*, from the cache when there is one.

    Measured on nixpkgs unstable: the walk costs 15 s and 2.08 GB, and reading
    the cache costs 0.1 s. Pass *refresh* to walk again and overwrite.
    """
    identity = await package_identity(pkgs_value)
    path = cache_path(identity)
    if not refresh:
        cached = load_cache(path)
        if cached is not None:
            return cached
    records = await fetch_package_list(session, pkgs_value, lib_value)
    save_cache(path, identity, records)
    return records
