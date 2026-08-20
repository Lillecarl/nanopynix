"""The layer that turns a command class into an argparse parser.

Issue #222 made this layer a library, and this suite is the first that reads
it directly. It ran under ``pynix`` before, through the commands of that
program and through the pty suite at ``pynix/completions/tests/``: both prove
the layer works for the options ``pynix`` happens to declare, and neither says
what the layer promises to a program that declares different ones.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal

import pytest

from libpynix import MISSING, Command, Spec, build_parser, command_name, dispatch, group, opt, pos


class Leaf(Command):
    """A leaf command."""

    name_of_thing: str | None = opt(None, short="n", help="A value.")
    count: int = opt(0, help="A number.")
    where: Path | None = opt(None, help="A path.")
    loud: bool = opt(False, help="A flag.")
    colour: bool = opt(True, negatable=True, help="A flag with both halves.")
    tags: list[str] = opt(help="A repeated value.")


def parse(command: type[Command], *arguments: str) -> Command:
    """Build *command* from *arguments*, the way a program's ``main`` does."""
    parser = build_parser(command)
    return dispatch(parser, parser.parse_args(arguments))


def test_a_class_name_becomes_a_kebab_case_command_name() -> None:
    class AddPath(Command):
        """Add a path."""

    assert command_name(AddPath) == "add-path"


def test_cli_name_overrides_the_class_name() -> None:
    class Whatever(Command):
        """Something."""

        cli_name = "path-info"

    assert command_name(Whatever) == "path-info"


def test_an_option_takes_its_long_and_its_short_spelling() -> None:
    assert parse(Leaf, "--name-of-thing", "a").name_of_thing == "a"  # type: ignore[attr-defined] -- dispatch answers with Command, and this test names the subclass
    assert parse(Leaf, "-n", "b").name_of_thing == "b"  # type: ignore[attr-defined] -- see above


def test_an_int_option_arrives_as_an_int() -> None:
    """argparse hands back a string unless it is told the type.

    Without it, ``--limit 20`` reached ``rapidfuzz`` as ``"20"`` and it
    answered ``TypeError: an integer is required``, six frames away from the
    declaration that says ``int``.
    """
    assert parse(Leaf, "--count", "20").count == 20  # type: ignore[attr-defined] -- see above


def test_a_path_option_arrives_as_a_path() -> None:
    assert parse(Leaf, "--where", "/nix/store").where == Path("/nix/store")  # type: ignore[attr-defined] -- see above


def test_a_flag_needs_no_value_and_a_negatable_flag_has_both_halves() -> None:
    assert parse(Leaf, "--loud").loud is True  # type: ignore[attr-defined] -- see above
    assert parse(Leaf, "--no-colour").colour is False  # type: ignore[attr-defined] -- see above
    assert parse(Leaf, "--colour").colour is True  # type: ignore[attr-defined] -- see above


def test_a_repeated_option_collects_every_occurrence() -> None:
    assert parse(Leaf, "--tags", "a", "--tags", "b").tags == ["a", "b"]  # type: ignore[attr-defined] -- see above


def test_a_repeated_option_gets_a_new_list_for_each_command() -> None:
    """A shared default would keep what a previous run appended to it."""
    first = parse(Leaf)
    second = parse(Leaf)
    first.tags.append("a")  # type: ignore[attr-defined] -- see above
    assert second.tags == []  # type: ignore[attr-defined] -- see above


def test_an_option_nobody_named_takes_the_declared_default() -> None:
    command = parse(Leaf)
    assert command.name_of_thing is None  # type: ignore[attr-defined] -- see above
    assert command.count == 0  # type: ignore[attr-defined] -- see above
    assert command.loud is False  # type: ignore[attr-defined] -- see above
    assert command.colour is True  # type: ignore[attr-defined] -- see above


def test_an_option_nobody_named_is_absent_from_the_namespace() -> None:
    """Every option is declared with ``argparse.SUPPRESS``.

    That is what lets a program ask which options the caller really typed,
    without a sentinel value standing in for "unset".
    """
    parser = build_parser(Leaf)
    named = vars(parser.parse_args(["--count", "3"]))
    assert set(named) == {"count", "_command"}


def test_a_configured_option_is_left_for_the_program_to_fill() -> None:
    class Configured(Command):
        """Takes a value from somewhere else."""

        store: str = opt("ignored", configured=True, help="A store.")

    assert parse(Configured).store is None  # type: ignore[attr-defined] -- see above


class Positionals(Command):
    """A command with each kind of positional."""

    required: str = pos(help="Must be given.")
    optional: str | None = pos(default=None, help="Need not be.")
    rest: list[str] = pos(help="Whatever is left.")


def test_a_positional_with_no_default_is_required() -> None:
    parser = build_parser(Positionals)
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_the_positionals_arrive_in_the_order_they_are_declared() -> None:
    command = parse(Positionals, "one", "two", "three", "four")
    assert command.required == "one"  # type: ignore[attr-defined] -- see above
    assert command.optional == "two"  # type: ignore[attr-defined] -- see above
    assert command.rest == ["three", "four"]  # type: ignore[attr-defined] -- see above


def test_an_omitted_optional_positional_takes_its_declared_default() -> None:
    command = parse(Positionals, "one")
    assert command.optional is None  # type: ignore[attr-defined] -- see above
    assert command.rest == []  # type: ignore[attr-defined] -- see above


def test_a_subclass_inherits_the_options_of_its_base() -> None:
    """A program puts its own base between ``Command`` and its commands."""

    class Base(Command):
        """Declares one option for every command under it."""

        store: str | None = opt(None, help="A store.")

    class Child(Base):
        """And one of its own."""

        attr: str | None = opt(None, help="An attribute.")

    assert set(Child.specs) == {"store", "attr"}
    command = parse(Child, "--store", "daemon", "--attr", "hello")
    assert command.store == "daemon"  # type: ignore[attr-defined] -- see above
    assert command.attr == "hello"  # type: ignore[attr-defined] -- see above


def test_an_option_that_shadows_a_name_of_this_layer_is_refused() -> None:
    """``pynix store add-path`` declares ``--name``, and that is why.

    With the name attribute called ``name``, that command's own declaration
    shadowed it and the subparser was registered under the repr of a ``Spec``.
    """
    with pytest.raises(TypeError, match=r"declares \['subcommands'\]"):
        # `type`, and not a `class` statement: the declaration under test
        # overrides a class variable of `Command`, which is exactly the fault
        # it reports, and pyright reads a written-out `class` as the mistake
        # rather than as the subject.
        type(
            "Bad",
            (Command,),
            {"__annotations__": {"subcommands": str | None}, "subcommands": opt(None, help="A collision.")},
        )


def test_a_group_mounts_its_children_and_runs_none_itself() -> None:
    mounted = group("store", help="Store commands.", subcommands=[Leaf])
    command = parse(mounted, "leaf", "-n", "a")
    assert isinstance(command, Leaf)
    assert command.name_of_thing == "a"


def test_a_group_named_with_no_subcommand_prints_its_help_and_stops() -> None:
    """A caller who names a group and stops has asked for help."""
    mounted = group("store", help="Store commands.", subcommands=[Leaf])
    parser = build_parser(mounted)
    with pytest.raises(SystemExit) as raised:
        dispatch(parser, parser.parse_args([]))
    assert raised.value.code == 0


def test_the_help_of_a_subcommand_is_the_first_line_of_its_docstring() -> None:
    mounted = group("root", help="Root.", subcommands=[Leaf])
    assert "A leaf command." in build_parser(mounted).format_help()


def test_a_declaration_is_a_spec_until_the_parser_reads_it() -> None:
    """The guard for every test above: they all lean on this shape."""
    assert isinstance(Leaf.specs["count"], Spec)
    assert Leaf.types["count"] is int
    assert pos(help="x").default is MISSING


def test_the_parser_is_an_ordinary_argparse_parser() -> None:
    """Nothing here subclasses argparse, so a program keeps every escape."""
    assert type(build_parser(Leaf)) is argparse.ArgumentParser


class Converted(Command):
    """A command whose positionals and repeated options are not strings."""

    where: Path = pos(help="A path the caller must give.")
    counts: list[int] = pos(help="Numbers, however many.")
    roots: list[Path] = opt(help="A repeated path.")


def test_a_positional_arrives_as_its_annotated_type() -> None:
    """A positional needs `type` exactly as an option does.

    Latent in `pynix`, where every `pos()` is a `str` on purpose, and live in
    `easykubenix`, which has three `Path` positionals. Issue #222.
    """
    command = parse(Converted, "/nix/store", "1", "2")
    assert command.where == Path("/nix/store")  # type: ignore[attr-defined] -- see above
    assert command.counts == [1, 2]  # type: ignore[attr-defined] -- see above


def test_a_repeated_option_converts_each_value() -> None:
    """argparse applies `type` to each value it collects."""
    command = parse(Converted, "/a", "--roots", "/x", "--roots", "/y")
    assert command.roots == [Path("/x"), Path("/y")]  # type: ignore[attr-defined] -- see above


class Required(Command):
    """A command with an option the caller must name."""

    to: str = opt(help="Where to push.", required=True)
    attr: str | None = opt(None, help="What to push.")


def test_a_required_option_must_be_named() -> None:
    parser = build_parser(Required)
    with pytest.raises(SystemExit):
        parser.parse_args(["--attr", "hello"])


def test_a_required_option_arrives_like_any_other() -> None:
    assert parse(Required, "--to", "s3://cache").to == "s3://cache"  # type: ignore[attr-defined] -- see above


def test_an_option_cannot_be_both_required_and_configured() -> None:
    """A configured option has a source below the command line.

    Requiring one at the parser would refuse a value that the environment or
    the configuration file already gives.
    """
    with pytest.raises(ValueError, match="required and configured"):
        opt(help="A contradiction.", required=True, configured=True)


def test_a_flag_is_not_given_a_type() -> None:
    """The guard for the conversion above: `bool` is in no `type=`.

    `store_true` and `BooleanOptionalAction` set the value themselves, and a
    `type` beside either one is an argparse error rather than a silent one.
    """
    assert parse(Leaf, "--loud").loud is True  # type: ignore[attr-defined] -- see above
    assert parse(Leaf, "--no-colour").colour is False  # type: ignore[attr-defined] -- see above


class Chosen(Command):
    """A command whose options name a fixed set of words."""

    style: Literal["yaml11", "yaml12"] = opt("yaml12", help="Which YAML version to write.")
    kinds: list[Literal["a", "b"]] = opt(help="A repeated choice.")
    which: Literal["one", "two"] = pos(help="A positional choice.")


def test_a_literal_becomes_the_set_of_words_the_parser_checks() -> None:
    """`pynix` writes its eight verbosity names in prose and checks none.

    A `Literal` says the set once, so the parser refuses anything else and the
    shell offers the words. Issue #222.
    """
    assert parse(Chosen, "one", "--style", "yaml11").style == "yaml11"  # type: ignore[attr-defined] -- see above

    parser = build_parser(Chosen)
    with pytest.raises(SystemExit):
        parser.parse_args(["one", "--style", "yaml13"])


def test_a_literal_positional_is_checked_too() -> None:
    assert parse(Chosen, "two").which == "two"  # type: ignore[attr-defined] -- see above
    with pytest.raises(SystemExit):
        build_parser(Chosen).parse_args(["three"])


def test_a_repeated_literal_checks_each_value() -> None:
    assert parse(Chosen, "one", "--kinds", "a", "--kinds", "b").kinds == ["a", "b"]  # type: ignore[attr-defined] -- see above
    with pytest.raises(SystemExit):
        build_parser(Chosen).parse_args(["one", "--kinds", "c"])


def test_the_words_reach_the_help() -> None:
    """The half a shell reads: argparse prints `choices` in the usage line."""
    assert "yaml11" in build_parser(Chosen).format_help()


def test_an_option_with_choices_is_not_also_given_a_type() -> None:
    """The guard: a `Literal` is not in `_CONVERTED`, so the two never meet."""
    parser = build_parser(Chosen)
    style = next(a for a in parser._actions if a.dest == "style")
    assert style.type is None
    assert style.choices == ("yaml11", "yaml12")
