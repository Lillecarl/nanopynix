"""Where a repeated ``pynix`` option gets its default from.

Four layers, and the first one that names a value wins:

1. the flag on the command line
2. the environment, as ``PYNIX_<OPTION>`` and ``PYNIX_NIX_<SETTING>``
3. ``$XDG_CONFIG_HOME/pynix/config.toml``
4. the built-in default

clypi resolves a flag before an environment variable before
``default_factory``, so the file plugs into ``default_factory`` and the parser
gives that order. Nothing here decides it, which is why the order cannot drift
away from what the help text says.

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

**The blocking file read is deliberate.** ``default_factory`` runs inside
``Pynix.parse()``, which ``main()`` calls before it starts any event loop, so
there is no loop to block and ``anyio.Path`` would have nowhere to run.
"""

from __future__ import annotations

import os
import tomllib
from contextlib import contextmanager

# A real import, not a TYPE_CHECKING one: the option factories below return
# clypi ``arg()`` placeholders, and clypi resolves the annotations of a command
# at run time to build its parser.
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast, override

from clypi import arg
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from nanopynix import NixSettingsEnv
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Generator

    from pydantic.fields import FieldInfo

#: What every ``--store`` used to default to, once per module.
DEFAULT_STORE = "auto"
DEFAULT_SUBSTITUTERS = ("https://cache.nixos.org/",)
DEFAULT_TRUSTED_PUBLIC_KEYS = ("cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=",)

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
        """
        return (init_settings, env_settings, _ConfigFileSource(settings_cls, cls.config_table))


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

    The two substituter fields carry the defaults that ``pynix build`` used to
    hold as its own constants. They are ordinary field defaults here, so the
    file and the environment both beat them -- which they did not, when the
    command passed its constants to the model as keyword arguments and the init
    source outranked the environment.
    """

    # Taken from the parent, and derived rather than written out again.
    # pydantic merges `model_config` over the bases in declaration order, so
    # `_TableBackedSettings` -- which carries only the `BaseSettings` defaults
    # -- resets whatever the parent set. Measured without this line:
    # `env_prefix` became "", so no `PYNIX_NIX_*` variable was read at all and
    # the environment lost to the configuration file with no message.
    model_config = SettingsConfigDict(**NixSettingsEnv.model_config)
    config_table: ClassVar[str] = NIX_TABLE

    substituters: list[str] | None = Field(default_factory=lambda: list(DEFAULT_SUBSTITUTERS))
    trusted_public_keys: list[str] | None = Field(default_factory=lambda: list(DEFAULT_TRUSTED_PUBLIC_KEYS))


_defaults: PynixDefaults | None = None


def defaults() -> PynixDefaults:
    """The resolved ``[defaults]``, read once per process.

    Cached because clypi calls ``default_factory`` once for each option it
    fills, and every one of them would otherwise read the file again.
    """
    global _defaults  # noqa: PLW0603 -- one process-wide cache, reset by the test fixture below
    if _defaults is None:
        _defaults = PynixDefaults()
    return _defaults


def nix_settings(**overrides: Any) -> PynixNixSettings:
    """The resolved ``[nix]``, with the options a command was given on top.

    A ``None`` override is dropped rather than applied, so an option the caller
    left alone keeps whatever the environment or the file said. This is the
    whole of the ordering: what reaches ``overrides`` is what a flag named.
    """
    named = {key: value for key, value in overrides.items() if value is not None}
    return PynixNixSettings(**named)


def reset_cache() -> None:
    """Forget the cached ``[defaults]``. For a test that changes the environment."""
    global _defaults  # noqa: PLW0603 -- see defaults()
    _defaults = None


@contextmanager
def only_built_in_defaults() -> Generator[None]:
    """Answer every option with its built-in default, inside this block.

    ``docs/pynix/reference.md`` prints the default of each option, and it is a
    checked-in file with a gate over it. Every ``default_factory`` here reads
    the configuration file and the environment, so without this the generated
    reference would say whatever the machine that ran the generator had
    configured -- and the gate would then fail for the one developer who used
    the feature that the reference documents.
    """
    global _defaults  # noqa: PLW0603 -- see defaults()
    previous = _defaults
    _defaults = PynixDefaults.model_construct()
    try:
        yield
    finally:
        _defaults = previous


@no_runtime_type_check  # clypi's arg() returns a PartialConfig placeholder at
# declaration time, not the annotated type -- see pynix.target.file_option
def store_option(help: str = "Store URI to use.") -> str:  # noqa: A002 -- clypi names the parameter `help`
    return arg(default_factory=lambda: defaults().store, help=help)


@no_runtime_type_check  # see store_option
def eval_store_option() -> str | None:
    return arg(
        default_factory=lambda: defaults().eval_store,
        help="Store URI to evaluate with. Defaults to --store.",
    )


@no_runtime_type_check  # see store_option
def verbosity_option() -> str | None:
    return arg(
        default_factory=lambda: defaults().verbosity,
        help="Nix log verbosity: error, warn, notice, info, talkative, chatty, debug, vomit, or 0-7.",
    )


@no_runtime_type_check  # see store_option
def print_build_logs_option() -> bool:
    return arg(
        default_factory=lambda: defaults().print_build_logs,
        # Snake case, not the dashed spelling: clypi normalises the parsed
        # option to snake case before it compares against this. The negative
        # exists because the default can now be true, and an option that a
        # configuration file turns on must stay possible to turn off.
        negative="no_print_build_logs",
        help="Print build log lines to stderr.",
    )
