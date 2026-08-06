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
import os
import sys
import tomllib

# A real import, not a TYPE_CHECKING one: the option factories below return
# clypi ``arg()`` placeholders, and clypi resolves the annotations of a command
# at run time to build its parser.
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn, cast, override

from clypi import Command, arg
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from nanopynix import NixSettingsEnv, PrefixedEnvSettingsSource
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check

if TYPE_CHECKING or BEARTYPING:
    from pydantic.fields import FieldInfo

#: What every ``--store`` used to default to, once per module.
DEFAULT_STORE = "auto"

#: The table of the configuration file that each settings model reads.
DEFAULTS_TABLE = "defaults"
NIX_TABLE = "nix"

#: Names another configuration file, for a test and for a user who keeps more
#: than one profile.
CONFIG_PATH_VARIABLE = "PYNIX_CONFIG"


def config_path() -> Path:
    """The configuration file this process reads, whether or not it exists."""
    override = os.environ.get(CONFIG_PATH_VARIABLE)
    if override:
        return Path(override)
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "pynix" / "config.toml"


class ConfigFileError(Exception):
    """The configuration file exists and cannot be read."""


def _read_table(table: str) -> dict[str, Any]:
    """Read one table out of the configuration file.

    A missing file is not an error: a user who never wrote one is the ordinary
    case. A file that exists and does not parse is an error, and the message
    names the file, because the alternative is a silently ignored setting.
    """
    path = config_path()
    try:
        text = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigFileError(f"cannot read {path}: {exc}") from exc
    try:
        document = tomllib.loads(text.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"cannot parse {path}: {exc}") from exc
    section = document.get(table)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigFileError(f"[{table}] in {path} must be a table, not {type(section).__name__}")
    # A TOML key is a string by the grammar, so the isinstance check above is
    # the whole of the narrowing that this needs.
    return cast("dict[str, Any]", section)


class _ConfigFileSource(PydanticBaseSettingsSource):
    """A settings source over one table of the configuration file.

    ``TomlConfigSettingsSource`` reads a whole document into one model, and
    this file holds two models in two tables. This reads the table it is told
    to, and leaves the key spelling to the model: every field carries a dashed
    alias, and ``populate_by_name`` accepts the Python name beside it.
    """

    def __init__(self, settings_cls: type[BaseSettings], table: str) -> None:
        super().__init__(settings_cls)
        self._table = table

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        raise NotImplementedError(f"{type(self).__name__} reads the whole table, not one field")

    def __call__(self) -> dict[str, Any]:
        return _read_table(self._table)


class _TableBackedSettings(BaseSettings):
    """A settings model whose lowest layer is one table of the configuration file.

    Both models below state the precedence by inheriting this, rather than each
    repeating the source order. The order is the whole contract of this module,
    so it exists once.
    """

    #: Which table of the configuration file this model reads.
    config_table: ClassVar[str]

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """What a caller passed, then the environment, then the file.

        The built-in default of each field is last without being listed:
        pydantic uses it when no source names the field.

        ``PrefixedEnvSettingsSource`` replaces the environment source that
        pydantic-settings builds. Every model here gives its fields a dashed
        alias, and pydantic-settings reads an alias as a second, unprefixed
        environment name. ``store`` is the one that matters most in this
        model: it is a common word, and it names which store a command opens.
        The class of nanopynix carries the measurement.
        """
        return (
            init_settings,
            PrefixedEnvSettingsSource(settings_cls),
            _ConfigFileSource(settings_cls, cls.config_table),
        )


class PynixDefaults(_TableBackedSettings):
    """The ``pynix`` options that repeat across the commands.

    These are not Nix settings. ``--store`` names which store a command opens,
    and Nix has a global of the same name that says something else, so this
    stays a separate model rather than another field of :class:`NixSettingsEnv`.
    """

    model_config = SettingsConfigDict(
        env_prefix="PYNIX_",
        alias_generator=lambda name: name.replace("_", "-"),
        populate_by_name=True,
        extra="forbid",
    )
    config_table: ClassVar[str] = DEFAULTS_TABLE

    store: str = DEFAULT_STORE
    eval_store: str | None = None
    verbosity: str | None = None
    print_build_logs: bool = False


class PynixNixSettings(NixSettingsEnv, _TableBackedSettings):
    """The Nix settings of a ``pynix`` command, from ``[nix]`` and ``PYNIX_NIX_*``.

    **This class states its own source order, although both of its bases state
    one.** ``NixSettingsEnv`` comes first in the method resolution order, and
    its order names no configuration file, so inheriting it dropped the
    ``[nix]`` table. Three tests catch that, and
    ``test_every_table_backed_model_still_reads_its_table`` is the one that
    names the reason rather than a symptom.

    It adds no field, and it holds no default. ``pynix`` used to give
    ``substituters`` and ``trusted-public-keys`` a default of its own, and a
    session sends every setting that is not ``None``, so a host that named a
    private cache in ``nix.conf`` lost that cache to ``pynix build`` with no
    message. A session now sends only what a caller named, so a default here
    could never take effect again -- and a default that cannot take effect is a
    false statement about the program.

    ``nix build`` carries no such default either. What ``nix.conf`` says stands,
    and this model speaks only when the file, the environment, or a flag names a
    setting.
    """

    # Taken from the parent, and derived rather than written out again.
    # pydantic merges `model_config` over the bases in declaration order, so
    # `_TableBackedSettings` -- which carries only the `BaseSettings` defaults
    # -- resets whatever the parent set. Measured without this line:
    # `env_prefix` became "", so no `PYNIX_NIX_*` variable was read at all and
    # the environment lost to the configuration file with no message.
    model_config = SettingsConfigDict(**NixSettingsEnv.model_config)
    config_table: ClassVar[str] = NIX_TABLE

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """The order of ``_TableBackedSettings``, and not the one of ``NixSettingsEnv``.

        ``super(NixSettingsEnv, cls)`` starts the search after the base that
        wins by default, so it reaches ``_TableBackedSettings`` with ``cls``
        still bound to this class. ``cls.config_table`` is what needs that
        binding: the base itself declares the name and holds no value for it.
        """
        return super(NixSettingsEnv, cls).settings_customise_sources(
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


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

#: The models an option resolves through, in priority order. The first model
#: that declares a name owns it, and that matters for exactly one name:
#: ``store``. :class:`PynixDefaults` means the store a command opens, and
#: :class:`PynixNixSettings` inherits Nix's unrelated global of the same name.
#: A command's ``--store`` is always the first. Without first-wins ownership the
#: second model would resolve ``self.store`` a second time, to ``None``.
_MODELS: tuple[type[BaseSettings], ...] = (PynixDefaults, PynixNixSettings)


def nix_settings(**overrides: Any) -> PynixNixSettings:
    """The resolved ``[nix]``, with the options a command was given on top.

    A ``None`` override is dropped rather than applied, so an option the caller
    left alone keeps whatever the environment or the file said. This is the
    whole of the ordering: what reaches ``overrides`` is what a flag named.

    A Nix setting that takes a list stays a plain flag of one string, and does
    not go through :func:`option`. The flag spells such a setting the way
    ``nix.conf`` does, in one space-separated string, and the model splits it.
    :data:`UNSET` would make the resolved list reach the attribute, where the
    command declares a string.
    """
    named = {key: value for key, value in overrides.items() if value is not None}
    return PynixNixSettings(**named)


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


def _configured_fields(command: type[Command]) -> dict[type[BaseSettings], list[str]]:
    """The options of *command* that :func:`option` declared, grouped by owner.

    The declaration is the whole test: an option whose ``default_factory``
    returns :data:`UNSET` is configuration-backed, and every other option is an
    ordinary flag with an ordinary default. Reading the declaration keeps a name
    that a model happens to share, such as ``substituters``, out of this.
    """
    owned: dict[type[BaseSettings], list[str]] = {}
    for field, conf in command.options().items():
        factory = conf.default_factory
        if not callable(factory) or factory() is not UNSET:
            continue
        for model in _MODELS:
            if field in model.model_fields:
                owned.setdefault(model, []).append(field)
                break
        else:
            raise TypeError(f"{command.__name__}.{field} uses option(), and no settings model declares it")
    return owned


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
        owned = _configured_fields(type(self))
        named: dict[str, Any] = {
            field: value
            for fields in owned.values()
            for field in fields
            if (value := getattr(self, field, UNSET)) is not UNSET
        }
        #: The options the caller named on the command line. Neither an
        #: environment variable nor the configuration file puts a name in here,
        #: which is what makes this the right question for
        #: ``Build._resolve_namespaced``.
        #:
        #: Declared here, and not in the class body: the clypi metaclass reads
        #: every annotation of the body as a command-line option, and it then
        #: refuses `frozenset[str]` because it has no parser for one.
        self._explicit: frozenset[str] = frozenset(named)
        for model, fields in owned.items():
            resolved = model(**{field: named[field] for field in fields if field in named})
            for field in fields:
                setattr(self, field, getattr(resolved, field))


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
