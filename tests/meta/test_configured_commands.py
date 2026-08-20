"""Gate: a command that declares a configuration-backed option resolves it.

``pynix._settings.option`` marks an option as configuration-backed, and
``ConfiguredCommand.__init__`` is the only thing that fills one in. A command
that declares such an option and inherits plain ``libpynix.Command``
therefore carries ``None`` into its ``run()``, and passes it to the library.

Nothing else catches that. The parser is happy, the type checker sees the
declared return type of ``store_option``, and the failure appears at the store
that the option names. This test walks the live command tree and states the
rule instead.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from libpynix import Command, build_parser
from pynix import Pynix
from pynix._impl.settings import configured_fields
from pynix._settings import ConfiguredCommand


def _command_tree(cmd: type[Command]) -> list[type[Command]]:
    """Every command reachable from *cmd*, including *cmd* itself."""
    found = [cmd]
    for sub in cmd.subcommands:
        found += _command_tree(sub)
    return found


def _configuration_backed_options(cmd: type[Command]) -> list[str]:
    """The options of *cmd* that ``option()`` declared.

    Read from the declaration, and not from a built instance: the point of the
    test is the command that never fills one in.
    """
    return [field for field, spec in cmd.specs.items() if spec.configured]


def test_every_command_with_a_configured_option_resolves_it() -> None:
    """The whole mitigation for the sentinel. See ``pynix._settings.option``."""
    offenders = {
        cmd.__name__: options
        for cmd in _command_tree(Pynix)
        if (options := _configuration_backed_options(cmd)) and not issubclass(cmd, ConfiguredCommand)
    }

    assert offenders == {}, "these commands declare a configured option and do not inherit ConfiguredCommand"


def test_every_configured_option_names_a_settings_field() -> None:
    """``option()`` on a name that no settings model declares has no default to
    resolve, so ``configured_fields`` refuses it. Prove that no command does
    this, rather than leaving the refusal to the first caller."""
    for cmd in _command_tree(Pynix):
        try:
            configured_fields(cmd)
        except TypeError as exc:
            pytest.fail(str(exc))


def test_the_command_tree_is_not_empty() -> None:
    """A guard for the two tests above, which both pass over an empty tree."""
    tree = _command_tree(Pynix)

    assert len(tree) > 20
    assert any(_configuration_backed_options(cmd) for cmd in tree)


def test_the_parser_writes_a_usage_failure_to_stderr() -> None:
    """The stream rule, which argparse keeps and clypi did not.

    stdout carries the answer of a command, so a caller can write ``pynix
    derivation show ... | jq``. A usage message is not an answer.

    ``clypi.Command.print_help`` wrote to stdout whether the caller asked for
    help or mistyped the command line, so every pynix command overrode the
    second case and this test walked the tree to prove none had been missed.
    ``ArgumentParser.error`` writes to stderr and exits 2, so there is one
    behaviour to check rather than fifty. Issue #214.

    Measured before the override existed: ``pynix derivation show <path>`` put
    2165 bytes on stdout and left stderr empty.
    """
    parser = build_parser(Pynix)
    out, err = io.StringIO(), io.StringIO()

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["build", "--no-such-option"])

    assert exit_info.value.code == 2
    assert out.getvalue() == "", "a usage failure reached stdout"
    assert "--no-such-option" in err.getvalue()


def test_asking_for_help_still_reaches_stdout() -> None:
    """The caller asked for it, so it is the answer of the command."""
    out = io.StringIO()

    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exit_info:
        build_parser(Pynix).parse_args(["build", "--help"])

    assert exit_info.value.code == 0
    assert "--attr" in out.getvalue()
