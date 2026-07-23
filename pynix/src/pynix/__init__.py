from __future__ import annotations

import functools
import operator

import rich.traceback
from clypi import Command

from nanopynix import set_manager_title
from pynix._util import configure_logging
from pynix.build import Build  # noqa: TC001
from pynix.config import Config  # noqa: TC001
from pynix.derivation import Derivation  # noqa: TC001
from pynix.eval import Eval  # noqa: TC001
from pynix.flake import Flake  # noqa: TC001
from pynix.log import Log  # noqa: TC001
from pynix.lsp import Lsp  # noqa: TC001
from pynix.osearch import Osearch  # noqa: TC001
from pynix.path_info import PathInfo  # noqa: TC001
from pynix.repl import Repl  # noqa: TC001
from pynix.store import Store  # noqa: TC001

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
    Flake,
    Log,
    Lsp,
    Osearch,
    PathInfo,
    Repl,
    Store,
]
if Ekn is not None:
    _subcommand_types.append(Ekn)

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
