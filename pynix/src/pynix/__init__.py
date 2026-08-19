from __future__ import annotations

import functools
import operator
from typing import TYPE_CHECKING

from pynix import _impl
from pynix._settings import PynixCommand
from pynix.build import Build
from pynix.config import Config
from pynix.derivation import Derivation
from pynix.develop import Develop, PrintDevEnv
from pynix.eval import Eval
from pynix.flake import Flake
from pynix.log import Log
from pynix.osearch import Osearch
from pynix.path_info import PathInfo
from pynix.repl import Repl
from pynix.store import Store

# **The union is built twice, and `pynix-lsp` is why.**
#
# clypi eval()s `Pynix.subcommand`'s annotation against this module's globals
# at class-body-execution time (`inspect.get_annotations(..., eval_str=True)`
# in `_CommandMeta._configure_subcommands`), so every member has to be a real
# bound name before `class Pynix` executes. An optional member therefore
# cannot be spliced in later.
#
# `pynix-lsp` holds the language server, so that `pygls`, `lsprotocol` and
# `jsonschema` are not dependencies of `pynix build`. Measured before the
# split: `import pynix` took 0.440 s and loaded 966 modules, 349 of them from
# those three. Issue #107.
#
# So the runtime builds the union from whatever imported, and the static half
# names every member unconditionally. pyright cannot type a runtime-computed
# value as a type expression (`reportInvalidTypeForm`), which is why the two
# halves exist rather than one.
#
# `tests/meta/test_subcommands.py` compares them, because two lists of the
# same thing drift.
if TYPE_CHECKING:
    from pynix_lsp.cli import Lsp

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
    )
else:
    _SUBCOMMANDS = [
        Build,
        Config,
        Eval,
        Derivation,
        Develop,
        Flake,
        Log,
        Osearch,
        PathInfo,
        PrintDevEnv,
        Repl,
        Store,
    ]
    try:
        from pynix_lsp.cli import Lsp
    except ImportError:
        # `pynix` runs without the server. `pynix lsp` is then not a
        # subcommand at all, rather than one that fails when it is called.
        pass
    else:
        _SUBCOMMANDS.append(Lsp)
    _PynixSubcommand = functools.reduce(operator.or_, _SUBCOMMANDS)


class Pynix(PynixCommand):
    """pynix — nanopynix CLI"""

    subcommand: _PynixSubcommand


def main() -> None:
    # `parse` first, and the set-up after it. clypi answers a shell completion
    # and `--help` inside `parse` and exits there, and neither needs a logger,
    # a process title or a traceback handler. `pynix._impl.main` holds the
    # measurement.
    cmd = Pynix.parse()
    _impl.main.prepare()
    cmd.start()


if __name__ == "__main__":
    main()
