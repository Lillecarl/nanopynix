"""The command-line layer of ``pynix``, as a library.

A program declares each command as a class, and this package builds the
argparse parser, answers a shell completion and dispatches::

    import asyncio

    from libpynix import Command, build_parser, complete, dispatch, opt

    class Render(Command):
        \"\"\"Render the evaluation result as YAML on stdout.\"\"\"

        attr: str | None = opt(None, short="A", help="Attribute path to render.")

        async def run(self) -> None:
            print(self.attr)

    class Tool(Command):
        \"\"\"A Nix command-line program.\"\"\"

        subcommands = (Render,)

    def main() -> None:
        parser = build_parser(Tool)
        complete(parser)
        asyncio.run(dispatch(parser, parser.parse_args()).run())

:mod:`libpynix.command` holds that layer, and nothing in it knows about Nix.
:mod:`libpynix.nix_options` holds the three options that every Nix CLI takes,
and declares them without reading them -- read that module for why the two
halves are apart.

Issue #222 made this a library. Before it, ``pynix/src/pynix/_cli.py`` was
359 lines and ``easykubenix``'s ``ekn/src/ekn/_cli.py`` was a copy of them.
"""

# pyright: reportUnusedImport=false
# Justifies the pragma above. The block below is this package's public surface:
# every import in it is a deliberate re-export that no runtime line reads, so
# 'unused' is what a correct entry looks like.
#
# Eager, and not the lazy PEP 562 table that `nanopynix_helpers` uses. The two
# modules below import argparse, inspect, dataclasses and pathlib and nothing
# else, so there is no cost for a lazy table to defer. `argcomplete` is the one
# import worth deferring, and `command.complete` already defers it.

from __future__ import annotations

from libpynix.command import (
    MISSING as MISSING,
    Command as Command,
    Completer as Completer,
    Spec as Spec,
    build_parser as build_parser,
    command_name as command_name,
    complete as complete,
    dispatch as dispatch,
    group as group,
    opt as opt,
    pos as pos,
)
from libpynix.nix_options import (
    attr_option as attr_option,
    file_option as file_option,
    flake_option as flake_option,
)
