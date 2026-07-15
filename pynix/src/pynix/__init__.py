from __future__ import annotations

from clypi import Command

from nanopynix import set_manager_title
from pynix._util import configure_logging
from pynix.build import Build  # noqa: TC001
from pynix.config import Config  # noqa: TC001
from pynix.derivation import Derivation  # noqa: TC001
from pynix.eval import Eval  # noqa: TC001
from pynix.flake import Flake  # noqa: TC001
from pynix.log import Log  # noqa: TC001
from pynix.path_info import PathInfo  # noqa: TC001
from pynix.repl import Repl  # noqa: TC001
from pynix.store import Store  # noqa: TC001


class Pynix(Command):
    """pynix — nanopynix CLI"""

    subcommand: Build | Config | Eval | Derivation | Flake | Log | PathInfo | Repl | Store


def main() -> None:
    set_manager_title("pynix")
    configure_logging()
    cmd = Pynix.parse()
    cmd.start()


if __name__ == "__main__":
    main()
