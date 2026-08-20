"""A command that takes the three options every Nix CLI takes.

`file_option`, `flake_option` and `attr_option` declare `--file`, `--flake`
and `--attr`. They declare them and read none of them, so this file needs no
evaluator: it prints what the caller named. A real program hands the three to
whatever resolves a target -- `pynix.target` is the one in this repository.

`libpynix/tests/test_documented_examples.py` runs this file.
"""

from __future__ import annotations

import asyncio
from typing import override

from libpynix import Command, attr_option, build_parser, dispatch, file_option, flake_option, opt


class Nix(Command):
    """The base that every command of this program inherits.

    A program puts its own base between `Command` and its commands, and this
    is where the options that cross every command belong.
    """

    file: str | None = file_option()
    flake: str | None = flake_option()
    attr: str | None = attr_option()


class Show(Nix):
    """Print the target that the options name."""

    json: bool = opt(False, help="Print the target as JSON.")

    @override
    async def run(self) -> None:
        named = {"file": self.file, "flake": self.flake, "attr": self.attr}
        print(named if self.json else " ".join(f"{k}={v}" for k, v in named.items()))


class Tool(Command):
    """A program that evaluates Nix."""

    subcommands = (Show,)


def main(arguments: list[str] | None = None) -> None:
    parser = build_parser(Tool)
    asyncio.run(dispatch(parser, parser.parse_args(arguments)).run())


if __name__ == "__main__":
    # See `minimal_example.py` for why the line is fixed.
    main(["show", "--flake", "nixpkgs#hello", "--attr", "version"])
