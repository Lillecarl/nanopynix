from __future__ import annotations

import rich.traceback

from nanopynix import set_manager_title
from pynix._settings import PynixCommand
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

# A plain module-level alias, and one union rather than two. It used to be
# built twice -- a runtime `functools.reduce(operator.or_, ...)` over a list
# and a separate static union under `if TYPE_CHECKING:` -- because `ekn` was
# an optional import here and pyright cannot type a runtime-computed value as
# a type expression (`reportInvalidTypeForm`). `ekn` is easykubenix's CLI and
# is no longer a dependency of this repository at all, so the union is static,
# the two constructions collapse into one, and the drift guard that compared
# them (tests/meta/test_subcommands.py) has nothing left to guard.
#
# clypi eval()s `Pynix.subcommand`'s annotation against this module's globals
# at class-body-execution time (`inspect.get_annotations(..., eval_str=True)`
# in `_CommandMeta._configure_subcommands`), so this has to be a real bound
# name before `class Pynix` executes.
_PynixSubcommand = (
    Build | Config | Eval | Derivation | Develop | Flake | Log | Lsp | Osearch | PathInfo | PrintDevEnv | Repl | Store
)


class Pynix(PynixCommand):
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
