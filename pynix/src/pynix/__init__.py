from __future__ import annotations

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

# **One union, written once.** clypi eval()s `Pynix.subcommand`'s annotation
# against this module's globals while the class body runs
# (`inspect.get_annotations(..., eval_str=True)` in
# `_CommandMeta._configure_subcommands`), so every member has to be a real
# bound name before `class Pynix` executes.
#
# It used to be written twice, because `pynix-lsp` was mounted here through an
# optional import and an optional member cannot be spliced in later. That
# mount is gone: `pynix-lsp` is its own program, which is what an editor calls
# and what sits beside `pynix` on the PATH of the dev shell. The alias cost a
# static union and a runtime one, a meta test to keep them in step, a third
# question in `checks.pynix-isolated`, and a dev shell that loaded 647 modules
# where a release build loads 202. Issues #107 and #123.
_PynixSubcommand = (
    Build | Config | Eval | Derivation | Develop | Flake | Log | Osearch | PathInfo | PrintDevEnv | Repl | Store
)


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
