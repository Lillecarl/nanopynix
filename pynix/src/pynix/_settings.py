"""Where a repeated ``pynix`` option gets its default from.

Four layers, and the first one that names a value wins:

1. the flag on the command line
2. the environment, as ``PYNIX_<OPTION>`` and ``PYNIX_NIX_<SETTING>``
3. ``$XDG_CONFIG_HOME/pynix/config.toml``
4. the built-in default

**pydantic-settings decides all four, and clypi decides none of them.** clypi
parses the command line and says which options the caller actually named;
those go into the model as keyword arguments, which is the init source, and
the environment and the file are the sources below it. One ordering, in one
library, stated once in :meth:`_TableBackedSettings.settings_customise_sources`.

An option the caller did not name holds :data:`UNSET` until
:class:`ConfiguredCommand` resolves it, which is how "absent" is told apart
from "explicitly false".

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
:meth:`ConfiguredCommand.__init__`, which ``Pynix.parse()`` calls, and ``main()``
calls ``parse()`` before it starts any event loop. There is no loop to block,
and ``anyio.Path`` would have nowhere to run.
"""

from __future__ import annotations

import contextlib
import sys

# A real import, not a TYPE_CHECKING one: the option factories below return
# clypi ``arg()`` placeholders, and clypi resolves the annotations of a command
# at run time to build its parser.
from typing import TYPE_CHECKING, Any, NoReturn, override

from clypi import Command, arg

from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from pynix import _impl

if TYPE_CHECKING or BEARTYPING:
    pass


class _Unset:
    """The value of a configuration-backed option that the caller did not name.

    A sentinel, and not ``None``, because of how clypi decides that an option is
    a flag. ``clypi/_cli/arg_config.py:16`` returns an argument count of zero
    only when the annotation *is* ``bool``; for ``bool | None`` it takes
    ``max([0, 1])`` and the option starts demanding a value, so ``--flag``
    fails with "Not enough values for flag".

    clypi never inspects what a ``default_factory`` returns, so a sentinel keeps
    the annotation ``bool``, keeps the flag a flag, and still separates "absent"
    from "explicitly false".
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


UNSET = _Unset()
"""The one instance. Compare against it with ``is``."""


@no_runtime_type_check  # clypi's arg() returns a PartialConfig placeholder at
# declaration time, not the annotated type -- see pynix.target.file_option
def option(help: str, **kwargs: Any) -> Any:  # noqa: A002 -- clypi names the parameter `help`
    """Declare an option whose default comes from the environment or the file.

    The declared default is :data:`UNSET`, always, so nothing about this
    declaration depends on the machine that runs it. That is what lets
    ``docs/pynix/reference.md`` stay the same file everywhere.

    A command that declares one of these must inherit :class:`ConfiguredCommand`,
    which is what turns the sentinel back into a value.
    ``tests/meta/test_configured_commands.py`` states that rule.
    """
    return arg(default_factory=lambda: UNSET, help=help, **kwargs)


class PynixCommand(Command):
    """The base of every pynix command, which owns which stream carries what.

    **stdout carries the answer of a command, and nothing else.** A caller
    writes ``pynix derivation show ... | jq``, so anything else on stdout
    arrives as data and breaks the reader. Everything a person reads --
    a log of Nix, an error, a usage message about a failure -- goes to stderr.

    ``clypi.Command.print_help`` writes to stdout for both of its cases, and
    ``ClypiConfig`` has no stream to set, so this overrides the case that is
    wrong. Measured before this class: ``pynix derivation show <path>``, which
    names no option that command takes, put 2165 bytes of usage table and a
    red error box on stdout and left stderr empty.

    ``--help`` keeps stdout. The caller asked for it, so it is the answer of
    the command rather than a report about a failure.
    """

    @classmethod
    @override
    def print_help(cls, exception: Exception | None = None) -> NoReturn:
        if exception is None:
            super().print_help(exception)
        # `redirect_stdout` rather than a copy of the formatter: clypi reads
        # `sys.stdout` when it writes, so this keeps one implementation of the
        # help text and moves only the stream it lands on.
        with contextlib.redirect_stdout(sys.stderr):
            super().print_help(exception)


class ConfiguredCommand(PynixCommand):
    """A command whose configuration-backed options resolve when it is built.

    The resolution happens in ``__init__`` rather than in each ``run``, so
    ``self.store`` is never :data:`UNSET` and no command had to change to read
    it. ``clypi.Command.__init__`` is an ordinary method, so this is a plain
    override.

    **The whole precedence lives in pydantic-settings.** What the caller typed
    goes in as keyword arguments, which is the init source; the environment and
    the file are the sources below it. clypi's only job is to say which options
    the caller named.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        owned = _impl.settings.configured_fields(type(self))
        named: dict[str, Any] = {
            field: value
            for fields in owned.values()
            for field in fields
            if (value := getattr(self, field, UNSET)) is not UNSET
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
        #: Declared here, and not in the class body: the clypi metaclass reads
        #: every annotation of the body as a command-line option, and it then
        #: refuses `frozenset[str]` because it has no parser for one.
        self.explicit_options: frozenset[str] = frozenset(named)
        for model, fields in owned.items():
            resolved = model(**{field: named[field] for field in fields if field in named})
            for field in fields:
                setattr(self, field, getattr(resolved, field))


# The three options that name what to evaluate. They live here, and not in
# `pynix.target` where the code that reads them lives, because clypi resolves
# the annotations of a command while its class body runs. A command module
# therefore imports whatever declares its options, and `pynix.target` pulls
# `structlog` and the exception tree of nanopynix -- 101 ms that a start
# which evaluates nothing was paying. Issue #123.
@no_runtime_type_check  # clypi's arg() returns a PartialConfig placeholder at declaration time, not the annotated type -- clypi's own machinery replaces it later; beartype would otherwise flag every call as a type violation
def file_option() -> str | None:
    """Declare the common ``--file`` option.

    The value is a string, and not a ``Path``. ``PurePath`` collapses a
    repeated separator, so ``https://example.com/x.tar.gz`` reached the
    evaluator as ``https:/example.com/x.tar.gz`` and failed. A reference is
    also not a path: ``github:NixOS/nixpkgs`` and ``<nixpkgs>`` name a tree
    that no local directory holds.
    """
    return arg(
        None,
        short="f",
        help="Evaluate FILE as a Nix expression. FILE is a path, a lookup path, a URL, or a flake reference, and it may end with '#' and an attribute path.",
    )


@no_runtime_type_check  # see file_option
def attr_option() -> str | None:
    """Declare the common ``--attr`` option."""
    return arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")


@no_runtime_type_check  # see file_option
def flake_option() -> str | None:
    """Declare the common ``--flake`` option."""
    return arg(None, help="Evaluate FLAKE, optionally with a '#'-separated attribute path.")


@no_runtime_type_check  # see option
def store_option(help: str = "Store URI to use.") -> str:  # noqa: A002 -- clypi names the parameter `help`
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
        # Snake case, not the dashed spelling: clypi normalises the parsed
        # option to snake case before it compares against this. The negative
        # exists because the default can be true, and an option that a
        # configuration file turns on must stay possible to turn off.
        negative="no_print_build_logs",
    )
