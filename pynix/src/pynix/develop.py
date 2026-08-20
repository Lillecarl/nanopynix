"""``pynix develop`` and ``pynix print-dev-env``, the counterparts of ``nix``'s.

Both commands compute the build environment of a derivation, which
``src/nix/develop.cc`` does in six steps: read the derivation, refuse a builder
that is not ``bash``, rewrite the derivation so that its builder is
``get-env.sh``, write the rewritten derivation, build it, and read the JSON
that the builder wrote.

The first four steps are one binding,
:meth:`~nanopynix.protocols.AsyncStore.write_dev_shell_derivation`, because the
three supported Nix versions disagree about how a derivation is written. The
last two are here. :mod:`pynix._dev_env` turns the JSON back into bash.

``develop`` takes its command after ``--``, and not from a ``--command``
option. argparse treats everything after the first ``--`` as a positional, so
``pynix develop -f . -- make -j4`` passes ``-j4`` through untouched. A pipeline
belongs inside the command, as ``-- bash -c 'make | less'``, exactly as with
``nix develop --command``.

**The tail is an ordinary positional now.** clypi kept it on the *class* and
offered no public way to clear it, so `pynix._impl.develop.take_unparsed`
reached into `clypi._cli.main.CLYPI_UNPARSED` to reset it between two parses in
one process. Issue #214 deleted both.
"""

from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._nix_options import attr_option, file_option, flake_option
from pynix._settings import (
    ConfiguredCommand,
    eval_store_option,
    print_build_logs_option,
    store_option,
    verbosity_option,
)


class PrintDevEnv(ConfiguredCommand):
    """Print the build environment of a derivation

    Examples:
      pynix print-dev-env --file default.nix --attr hello
      pynix print-dev-env --flake .#hello --json"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to build with.")

    eval_store: str | None = eval_store_option()

    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    json: bool = opt(False, help="Print the environment as JSON, instead of the bash that restores it.")

    @override
    async def run(self) -> None:
        await _impl.develop.run_print_dev_env(self)


class Develop(ConfiguredCommand):
    """Run a command, or an interactive bash, in a derivation's build environment

    Everything after -- is the command. Without a command, this starts an
    interactive bash.

    Examples:
      pynix develop --file default.nix --attr hello
      pynix develop --flake .# -- make -j4
      pynix develop --flake .# -- bash -c 'make | less'"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to build with.")

    eval_store: str | None = eval_store_option()

    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    #: Everything after ``--``. Empty for an interactive shell.
    command: list[str] = pos(help="Command to run in the environment, after --. Omit for an interactive bash.")

    @override
    async def run(self) -> None:
        await _impl.develop.run_develop(self)
