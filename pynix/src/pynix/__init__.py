from __future__ import annotations

import functools
import operator
from typing import TYPE_CHECKING

import rich.traceback
from clypi import Command

from nanopynix import set_manager_title
from pynix._util import configure_logging
from pynix.build import Build
from pynix.config import Config
from pynix.derivation import Derivation
from pynix.develop import Develop, PrintDevEnv
from pynix.eval import Eval
from pynix.flake import Flake
from pynix.log import Log
from pynix.lsp import Lsp
from pynix.osearch import Osearch
from pynix.path_info import PathInfo
from pynix.repl import Repl
from pynix.store import Store

# ekn is an optional runtime dependency: pynix/package.nix only bundles it
# into the built environment when its `ekn` arg is non-null. Guarded so a
# pynix build without ekn still imports and runs cleanly. clypi's
# `Pynix.subcommand` annotation is eval()'d against this module's globals at
# class-body-execution time (`inspect.get_annotations(..., eval_str=True)`
# in clypi's `_CommandMeta._configure_subcommands`), so the union has to be
# a real bound name *before* `class Pynix` executes -- it can't reference
# `Ekn` inline when the import fails, since `None` isn't a type clypi's
# subcommand-union check accepts. This is the one place pynix imports from
# ekn (see nanopynix/AGENTS.md's narrow-private-import rule).
try:
    from ekn.cli import Ekn
except ImportError:
    Ekn = None

_subcommand_types: list[type[Command]] = [
    Build,
    Config,
    Eval,
    Derivation,
    Develop,
    Flake,
    Log,
    Lsp,
    Osearch,
    PathInfo,
    PrintDevEnv,
    Repl,
    Store,
]
if Ekn is not None:
    _subcommand_types.append(Ekn)

# `functools.reduce(operator.or_, ...)` builds the real runtime union (its
# membership depends on whether `ekn.cli` imported successfully, above), but
# pyright can't type a runtime-computed value as a type expression at all
# (reportInvalidTypeForm). `ekn/src` is always on pyright's `extraPaths`
# (pyproject.toml) even though importing `ekn` at runtime is genuinely
# optional, so give the type checker a real static union that always
# includes `Ekn` -- it only sees the branch below, never the runtime one.
if TYPE_CHECKING:
    from ekn.cli import Ekn as _Ekn

    _PynixSubcommand = (
        Build
        | Config
        | Eval
        | Derivation
        | Develop
        | Flake
        | Log
        | Lsp
        | Osearch
        | PathInfo
        | PrintDevEnv
        | Repl
        | Store
        | _Ekn
    )
else:
    _PynixSubcommand = functools.reduce(operator.or_, _subcommand_types)


class Pynix(Command):
    """pynix — nanopynix CLI"""

    subcommand: _PynixSubcommand


def main() -> None:
    rich.traceback.install(show_locals=True)
    set_manager_title("pynix")
    configure_logging()
    cmd = Pynix.parse()
    cmd.start()


if __name__ == "__main__":
    main()
