"""The smallest program this library builds: one command, one option.

`libpynix/tests/test_examples.py` runs this file, so a change to the library
that breaks it fails the suite. That is the reason the documentation points at
a script rather than repeating it as a snippet.
"""

from __future__ import annotations

import asyncio
from typing import override

from libpynix import Command, build_parser, complete, dispatch, opt


class Greet(Command):
    """Print a greeting.

    The first line of this docstring is what `--help` prints beside the name
    of the command, and the whole docstring is what `greet --help` prints.
    """

    #: An annotated class attribute is an option. The annotation decides what
    #: the parser does with the value, and `opt` says the rest.
    name: str = opt("world", short="n", help="Who to greet.")
    loud: bool = opt(False, help="Shout it.")

    @override
    async def run(self) -> None:
        greeting = f"Hello, {self.name}!"
        print(greeting.upper() if self.loud else greeting)


class Tool(Command):
    """A program with one subcommand."""

    subcommands = (Greet,)


def main(arguments: list[str] | None = None) -> None:
    parser = build_parser(Tool)
    # Answers a shell completion and exits, when this start is one. A start
    # that is not a completion returns here at once and imports nothing.
    complete(parser)
    asyncio.run(dispatch(parser, parser.parse_args(arguments)).run())


if __name__ == "__main__":
    # A fixed line, so the file demonstrates itself when a reader runs it and
    # when the suite runs it. Call `main()` with no argument to read the real
    # command line instead.
    main(["greet", "--name", "reader"])
