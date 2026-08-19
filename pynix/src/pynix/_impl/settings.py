"""Where a repeated ``pynix`` option gets its default from.

Four layers, and the first one that names a value wins:

1. the flag on the command line
2. the environment, as ``PYNIX_<OPTION>`` and ``PYNIX_NIX_<SETTING>``
3. ``$XDG_CONFIG_HOME/pynix/config.toml``
4. the built-in default

**pydantic-settings decides all four, and the parser decides none of them.** argparse
parses the command line and says which options the caller actually named;
those go into the model as keyword arguments, which is the init source, and
the environment and the file are the sources below it. One ordering, in one
library, stated once in :meth:`_TableBackedSettings.settings_customise_sources`.

An option the caller did not name is absent from the namespace until
:class:`~pynix._settings.ConfiguredCommand` resolves it, which is how "absent"
is told apart from "explicitly false".

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
:meth:`~pynix._settings.ConfiguredCommand.__init__`, which ``Pynix.parse()``
calls, and ``main()`` calls ``parse()`` before it starts any event loop. There
is no loop to block, and ``anyio.Path`` would have nowhere to run.

**Why this is under ``pynix._impl`` and not beside the command base.**
:class:`PynixNixSettings` inherits the whole Nix settings model, so the class
statement below imports ``pydantic_settings`` and builds a model of about 200
fields. Building the parser loads every subcommand module on every start,
``pynix --help`` and each keypress of a shell completion included, and none of
those resolves a setting. Issue #123 measured ``pynix._settings`` at 334.3 ms,
of which ``pydantic_settings`` alone was 123.6 ms.

``ConfiguredCommand`` reaches these names when it builds a command, which is
after the parser decided that a command runs. ``pynix._impl`` says how.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast, override

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from nanopynix import NixSettingsEnv, PrefixedEnvSettingsSource
from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from pydantic.fields import FieldInfo

    from pynix._cli import Command


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
    Going through :func:`option` would make the resolved list reach the
    attribute, where the command declares a string.
    """
    named = {key: value for key, value in overrides.items() if value is not None}
    return PynixNixSettings(**named)


def configured_fields(command: type[Command]) -> dict[type[BaseSettings], list[str]]:
    """The options of *command* that :func:`option` declared, grouped by owner.

    The declaration is the whole test: an option that :func:`pynix._settings.option`
    declared carries ``configured``, and every other option is an ordinary flag
    with an ordinary default. Reading the declaration keeps a name that a model
    happens to share, such as ``substituters``, out of this.
    """
    owned: dict[type[BaseSettings], list[str]] = {}
    for field, spec in command.specs.items():
        if not spec.configured:
            continue
        for model in _MODELS:
            if field in model.model_fields:
                owned.setdefault(model, []).append(field)
                break
        else:
            raise TypeError(f"{command.__name__}.{field} uses option(), and no settings model declares it")
    return owned
