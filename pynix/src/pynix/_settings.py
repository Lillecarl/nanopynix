"""Where a repeated ``pynix`` option gets its default from.

Four layers, and the first one that names a value wins:

1. the flag on the command line
2. the environment, as ``PYNIX_<OPTION>`` and ``PYNIX_NIX_<SETTING>``
3. ``$XDG_CONFIG_HOME/pynix/config.toml``
4. the built-in default

**pydantic-settings decides all four, and the parser decides none of them.**
argparse says which options the caller actually named; those go into the model
as keyword arguments, which is the init source, and the environment and the
file are the sources below it. One ordering, in one library, stated once in
:meth:`_TableBackedSettings.settings_customise_sources`.

An option the caller did not name is simply **absent**, because every option is
declared with ``argparse.SUPPRESS``. That is how "absent" is told apart from
"explicitly false", and it is why the ``UNSET`` sentinel that clypi needed is
gone. Issue #214.

The file holds two tables. ``[defaults]`` holds the options that cross the
commands, and ``[nix]`` holds the Nix settings::

    [defaults]
    store = "daemon"
    verbosity = "notice"
    print-build-logs = true

    [nix]
    substituters = ["https://cache.nixos.org/", "https://mine.example/"]
    max-jobs = 8

``[nix]`` reaches the same model as ``PYNIX_NIX_*``, so a list takes the
``nix.conf`` spelling as well as a TOML array.

**The blocking file read is deliberate.** The read happens in
:meth:`ConfiguredCommand.__init__`, which ``main()`` calls when it builds the
command, and ``main()`` builds the command before it starts any event loop.
There is no loop to block, and ``anyio.Path`` would have nowhere to run.
"""

from __future__ import annotations

from typing import Any

from libpynix import Command, opt
from nanopynix._typechecking import no_runtime_type_check
from pynix import _impl


@no_runtime_type_check  # a declaration returns a Spec, and the annotation names the value it will hold
def option(help: str, **kwargs: Any) -> Any:  # noqa: A002 -- argparse names the parameter `help`
    """Declare an option whose default comes from the environment or the file.

    Nothing about this declaration depends on the machine that runs it, which
    is what lets ``docs/pynix/reference.md`` stay the same file everywhere.

    A command that declares one of these must inherit :class:`ConfiguredCommand`,
    which is what fills it in. ``tests/meta/test_configured_commands.py`` states
    that rule.
    """
    return opt(help=help, configured=True, **kwargs)


class PynixCommand(Command):
    """The base of every pynix command, which owns which stream carries what.

    **stdout carries the answer of a command, and nothing else.** A caller
    writes ``pynix derivation show ... | jq``, so anything else on stdout
    arrives as data and breaks the reader. Everything a person reads --
    a log of Nix, an error, a usage message about a failure -- goes to stderr.

    **argparse already does this, and clypi did not.** ``ArgumentParser.error``
    writes the usage line and the message to stderr and exits 2, and ``--help``
    writes to stdout because the caller asked for it. ``clypi.Command.print_help``
    wrote to stdout for both cases, so this class carried an override; issue
    #214 deleted it. Measured before that override existed: ``pynix derivation
    show <path>``, which names no option that command takes, put 2165 bytes of
    usage table and a red error box on stdout and left stderr empty.

    The class stays, because every command still shares a base and the rule
    still has to be written down somewhere.
    """


class ConfiguredCommand(PynixCommand):
    """A command whose configuration-backed options resolve when it is built.

    The resolution happens in ``__init__`` rather than in each ``run``, so
    ``self.store`` always holds a value and no command has to ask where it came
    from.

    **The whole precedence lives in pydantic-settings.** What the caller typed
    goes in as keyword arguments, which is the init source; the environment and
    the file are the sources below it. The parser's only job is to say which
    options the caller named, and ``argparse.SUPPRESS`` is how it says so: an
    option nobody named is not in *values* at all.
    """

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        owned = _impl.settings.configured_fields(type(self))
        named: dict[str, Any] = {
            field: values[field] for fields in owned.values() for field in fields if field in values
        }
        #: The options the caller named on the command line. Neither an
        #: environment variable nor the configuration file puts a name in here,
        #: which is what makes this the right question for
        #: ``pynix._impl.build._resolve_namespaced``.
        #:
        #: Public, and not ``_explicit``: the one caller is in another module
        #: now, because the body of ``Build.run`` moved out of the command
        #: module. See ``pynix._impl``.
        #:
        #: Declared here, and not in the class body: an annotated name in the
        #: body is a command-line option, and `frozenset[str]` is not one.
        self.explicit_options: frozenset[str] = frozenset(named)
        for model, fields in owned.items():
            resolved = model(**{field: named[field] for field in fields if field in named})
            for field in fields:
                setattr(self, field, getattr(resolved, field))


# `--file`, `--flake` and `--attr` are not here. They mean the same thing in
# every program that evaluates Nix, and `libpynix.nix_options` declares them
# for all of them since issue #222. That module also keeps the rule this file
# used to state: a declaration lives away from `pynix.target`, which is where
# the code that *reads* one lives, because a command module imports whatever
# declares its options and `pynix.target` pulls `structlog` and the exception
# tree of nanopynix -- 101 ms that a start which evaluates nothing was paying.
# Issue #123.
@no_runtime_type_check  # see option
def store_option(help: str = "Store URI to use.") -> str:  # noqa: A002 -- see option
    return option(help)


@no_runtime_type_check  # see option
def eval_store_option() -> str | None:
    return option("Store URI to evaluate with. Defaults to --store.")


@no_runtime_type_check  # see option
def verbosity_option() -> str | None:
    return option("Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7.")


@no_runtime_type_check  # see option
def print_build_logs_option() -> bool:
    return option(
        "Print build log lines to stderr.",
        # The negative exists because the default can be true, and an option
        # that a configuration file turns on must stay possible to turn off.
        # `argparse.BooleanOptionalAction` writes both halves.
        negatable=True,
    )
