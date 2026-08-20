"""The three options that name what to evaluate.

One declaration for both programs, which is the half of issue #222 that the
issue calls the part that matters most: the help of ``--attr`` was the same
string in ``pynix`` and in ``easykubenix``, character for character.
"""

from __future__ import annotations

from libpynix import Command, attr_option, build_parser, dispatch, file_option, flake_option


class Evaluating(Command):
    """A command that names what to evaluate."""

    file: str | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()


def parse(*arguments: str) -> Evaluating:
    parser = build_parser(Evaluating)
    command = dispatch(parser, parser.parse_args(arguments))
    assert isinstance(command, Evaluating)
    return command


def test_the_three_options_take_their_long_spelling() -> None:
    command = parse("--file", "./x.nix", "--attr", "a.b", "--flake", "nixpkgs#hello")
    assert command.file == "./x.nix"
    assert command.attr == "a.b"
    assert command.flake == "nixpkgs#hello"


def test_file_and_attr_have_the_short_spelling_that_nix_uses() -> None:
    command = parse("-f", "./x.nix", "-A", "a.b")
    assert command.file == "./x.nix"
    assert command.attr == "a.b"


def test_a_flake_reference_is_a_string_and_keeps_every_separator() -> None:
    """``PurePath`` collapses a repeated separator, and a URL is not a path.

    ``https://example.com/x.tar.gz`` reached the evaluator as
    ``https:/example.com/x.tar.gz`` and failed.
    """
    assert parse("--file", "https://example.com/x.tar.gz").file == "https://example.com/x.tar.gz"


def test_none_of_the_three_is_required() -> None:
    command = parse()
    assert (command.file, command.attr, command.flake) == (None, None, None)


def test_each_option_carries_a_help_line() -> None:
    """The guard: a declaration with no help is a Tab that says nothing."""
    for field in ("file", "attr", "flake"):
        assert Evaluating.specs[field].help.endswith(".")
