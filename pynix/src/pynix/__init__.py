from __future__ import annotations

# A real import, and not a `TYPE_CHECKING` one. `NANOPYNIX_BEARTYPING=1` makes
# beartype resolve every annotation at run time, and a name the type checker
# alone can see becomes a forward reference it cannot import. `collections.abc`
# is already loaded by the interpreter, so this costs nothing.
from collections.abc import Sequence

from libpynix import Command, build_parser, complete, dispatch, group
from pynix import _impl
from pynix.build import Build
from pynix.config import Config
from pynix.derivation import Derivation
from pynix.develop import Develop, PrintDevEnv
from pynix.eval import Eval
from pynix.flake import Flake
from pynix.log import Log
from pynix.path_info import PathInfo
from pynix.repl import Repl
from pynix.search import Search
from pynix.store import Store
from pynix.why_depends import WhyDepends

# **One list, and the order is the order `pynix --help` prints.**
#
# It used to be a union annotation, because clypi eval()s `Pynix.subcommand`
# against this module's globals while the class body runs. Issue #214 replaced
# clypi with argparse, so this is an ordinary list and every name in it is an
# ordinary import.
#
# `pynix-lsp` is not here. It is its own program, which is what an editor calls
# and what sits beside `pynix` on the PATH of the dev shell. The alias it used
# to have cost a static union and a runtime one, a meta test to keep the two in
# step, a third question in `checks.pynix-isolated`, and a dev shell that loaded
# 647 modules where a release build loads 202. Issues #107 and #123.
Pynix = group(
    "pynix",
    help="pynix — nanopynix CLI",
    subcommands=[
        Build,
        Config,
        Eval,
        Derivation,
        Develop,
        Flake,
        Log,
        PathInfo,
        PrintDevEnv,
        Repl,
        Search,
        Store,
        WhyDepends,
    ],
)


def parse(argv: Sequence[str]) -> Command:
    """The command that *argv* names, built and ready to run.

    What `main` does, without running anything. A test drives the real parser
    through this rather than a double, so a change to a declaration is a change
    the test sees.
    """
    parser = build_parser(Pynix)
    return dispatch(parser, parser.parse_args(list(argv)))


def main() -> None:
    # **The parse comes first, and the set-up after it.** A shell completion
    # and `--help` both end inside this function, and neither needs a logger, a
    # process title or a traceback handler. `pynix._impl.main` holds the
    # measurement.
    parser = build_parser(Pynix)
    complete(parser)
    command = dispatch(parser, parser.parse_args())
    _impl.main.prepare()
    _impl.main.run(command.run)


if __name__ == "__main__":
    main()
