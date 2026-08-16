"""Gate: a command that declares a configuration-backed option resolves it.

``pynix._settings.option`` gives an option the sentinel ``UNSET`` as its
default, and ``ConfiguredCommand.__init__`` is the only thing that turns the
sentinel back into a value. A command that declares such an option and inherits
plain ``clypi.Command`` therefore carries ``<unset>`` into its ``run()``, and
passes it to the library.

Nothing else catches that. clypi parses the command, the type checker sees the
declared return type of ``store_option`` and not the sentinel, and the failure
appears at the store that the option names. This test walks the live command
tree and states the rule instead.
"""

from __future__ import annotations

import pytest
from clypi import Command

from pynix import Pynix
from pynix._settings import UNSET, ConfiguredCommand, _configured_fields


def _command_tree(cmd: type[Command]) -> list[type[Command]]:
    """Every command reachable from *cmd*, including *cmd* itself."""
    found = [cmd]
    for sub in cmd.subcommands().values():
        if sub is not None:
            found += _command_tree(sub)
    return found


def _configuration_backed_options(cmd: type[Command]) -> list[str]:
    """The options of *cmd* that ``option()`` declared.

    Read from the declaration, and not from a built instance: the point of the
    test is the command that never resolves its sentinel.
    """
    named: list[str] = []
    for field, conf in cmd.options().items():
        factory = conf.default_factory
        if callable(factory) and factory() is UNSET:
            named.append(field)
    return named


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
    resolve, so ``_configured_fields`` refuses it. Prove that no command does
    this, rather than leaving the refusal to the first caller."""
    for cmd in _command_tree(Pynix):
        try:
            _configured_fields(cmd)
        except TypeError as exc:
            pytest.fail(str(exc))


def test_the_command_tree_is_not_empty() -> None:
    """A guard for the two tests above, which both pass over an empty tree."""
    tree = _command_tree(Pynix)

    assert len(tree) > 20
    assert any(_configuration_backed_options(cmd) for cmd in tree)


def test_every_command_writes_a_usage_failure_to_stderr() -> None:
    """The whole command tree obeys the stream rule, and not a part of it.

    ``clypi.Command.print_help`` writes to stdout whether the caller asked for
    help or mistyped the command line, and ``ClypiConfig`` names no stream, so
    a pynix command overrides the second case. A command that keeps clypi's
    own method puts its usage table in the stdout of whatever reads the
    command, and nothing reports that.

    Measured before the override existed: ``pynix derivation show <path>`` put
    2165 bytes on stdout and left stderr empty.

    **The subject is the method, and not a shared base class.** The rule is
    about behaviour, not inheritance: a command satisfies it by not keeping
    clypi's own ``print_help``, however it gets there.
    """
    offenders = sorted(
        cmd.__name__ for cmd in _command_tree(Pynix) if cmd.print_help.__func__ is Command.print_help.__func__
    )

    assert offenders == [], "these commands keep clypi's print_help, so a usage failure goes to stdout"
