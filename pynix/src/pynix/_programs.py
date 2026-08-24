"""The binary-name index that Hydra ships, and the one question it answers.

**"Which package gives me `rg`?" is the thing `nix search` cannot answer**, and
issue #85 calls it the single largest improvement a package search can make.
`pynix/_packages.py` gets close with `meta.mainProgram`, and stops short: that
field names one binary, and a package installs many. Measured against the real
index: `rg` resolves through `mainProgram`, and `ssh-keygen`, `convert`, `awk`
and `xxd` each resolve to nothing.

Hydra publishes the answer. `programs.sqlite` sits inside `nixexprs.tar.xz`,
holds 161 511 rows of binary name to package, and `builtins.fetchTarball`
reaches it in one call -- 13.0 s cold, and a store hit after that. The file is
17.0 MB, its schema is three text columns, and the standard library reads it,
so this module adds no dependency.

**The rows are per system.** Measured: 83 048 for `x86_64-linux`, 77 917 for
`aarch64-linux` and 546 for `i686-linux`. A query that ignores the system
answers for a machine the caller does not have.

**This index describes one channel release, and the package walk describes the
nixpkgs the caller pinned.** The two join on the package name, which is stable
across releases in a way a version is not. So a miss means "no binary is
known", and never a wrong answer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable, Mapping

    from nanopynix import AsyncEvalSession

#: Where Hydra publishes the channel artifacts.
CHANNEL_URL = "https://channels.nixos.org"

#: The expression that fetches and unpacks `nixexprs.tar.xz`.
#:
#: **The evaluation has to be impure.** A channel artifact is mutable, so its
#: hash is not known beforehand, and pure mode refuses: "in pure evaluation
#: mode, 'fetchurl' requires a 'sha256' argument". `fetchTarball` puts the
#: result in the store, so the garbage collector owns it and a second call
#: costs nothing.
_FETCH_NIXEXPRS = 'builtins.fetchTarball "{url}/{channel}/nixexprs.tar.xz"'


@dataclass(frozen=True)
class ProgramIndex:
    """The binary names of one channel release, for one system."""

    #: The `programs.sqlite` that `nixexprs.tar.xz` carries.
    path: Path

    #: The system whose rows this index answers for, such as `x86_64-linux`.
    system: str

    def packages_for_binary(self, binary: str) -> list[str]:
        """The packages that install a binary called *binary*, by attribute.

        This is the question the caller really asks. `ssh-keygen` gives
        `openssh`, and `convert` gives `imagemagick`, and neither package
        names that binary as its `mainProgram`.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "select package from Programs where name = ? and system = ? order by package",
                (binary, self.system),
            )
            return [str(row[0]) for row in rows]

    def binaries_for_packages(self, packages: Iterable[str]) -> Mapping[str, list[str]]:
        """The binaries that each of *packages* installs, by attribute.

        The caller passes the packages it is about to show, and not the whole
        index. A search shows a page of results, and this is what fills in
        "what arrives on PATH" for that page.
        """
        wanted = list(dict.fromkeys(packages))
        if not wanted:
            return {}
        found: dict[str, list[str]] = {package: [] for package in wanted}
        placeholders = ",".join("?" for _ in wanted)
        with self._connect() as connection:
            rows = connection.execute(
                f"select package, name from Programs where system = ? and package in ({placeholders}) order by name",  # noqa: S608 -- the placeholders are generated, and every value is bound
                (self.system, *wanted),
            )
            for package, name in rows:
                found[str(package)].append(str(name))
        return found

    def binaries_by_package(self) -> Mapping[str, list[str]]:
        """Every package of this system, with the binaries it installs.

        **This is the bulk join, and `binaries_for_packages` is the page one.**
        A query cannot bind 24 571 package names: SQLite bounds the number of
        variables in one statement, and the walk of nixpkgs returns more than
        that. So this reads the rows of the system in one pass and groups them
        here. Measured: 83 048 rows for `x86_64-linux`.
        """
        found: dict[str, list[str]] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "select package, name from Programs where system = ? order by package, name",
                (self.system,),
            )
            for package, name in rows:
                found.setdefault(str(package), []).append(str(name))
        return found

    def _connect(self) -> sqlite3.Connection:
        """Open the index read-only, because nothing here ever writes to it."""
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)


async def fetch_program_index(session: AsyncEvalSession, system: str, channel: str = "nixos-unstable") -> ProgramIndex:
    """Fetch the channel expressions, and point at the `programs.sqlite` in them.

    The fetch runs through the evaluator, so it uses the store and the cache
    that Nix already has. A second call for an unchanged release downloads
    nothing.
    """
    expression = _FETCH_NIXEXPRS.format(url=CHANNEL_URL, channel=channel)
    fetched = await session.string(expression)
    root = await fetched.to_python()
    if not isinstance(root, str):
        raise TypeError(f"fetchTarball must give a path, got {type(root).__name__}")
    database = Path(root) / "programs.sqlite"
    if not database.is_file():
        raise FileNotFoundError(f"the channel expressions hold no programs.sqlite at {database}")
    return ProgramIndex(path=database, system=system)
