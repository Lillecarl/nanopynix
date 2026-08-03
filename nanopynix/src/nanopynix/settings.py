"""Typed Nix configuration models."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast

import yaml
from nanopynix_bindings import (
    expr as nanopynix_expr,
    fetchers as nanopynix_fetchers,
    flake as nanopynix_flake,
    util as nanopynix_util,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix.exceptions import SettingNotLiveError, SettingOutOfScopeError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterator, Mapping, Sequence


DEFAULT_EXPERIMENTAL_FEATURES = (
    "flakes",
    "nix-command",
    "ca-derivations",
    "dynamic-derivations",
    "recursive-nix",
)
DEFAULT_LINE_EDITORS = ("emacs", "nano", "vim", "kak", "hx")
type SettingsSurface = Literal["global", "eval", "fetch", "flake"]


def _alias(field_name: str) -> str:
    return field_name.replace("_", "-")


def field_key(field_name: str, field: Any) -> str:
    """The nix.conf key a field is *written* as.

    Rendering and drift checking both go through this, so a field that has to
    diverge from the derived spelling stays correct in both. ``store_dir`` is
    the reason it exists: Nix registers it as ``store``, which is also the name
    of an unrelated global setting, so that field declares its own
    ``serialization_alias`` and this reports it.
    """
    return field.serialization_alias or _alias(field_name)


#: When Nix reads a setting, and therefore whether it can be changed later.
#: ``"live"`` is read at the point of use; ``"construction"`` is read once,
#: while the object that owns it is being built.
type Mutability = Literal["live", "construction"]

MUTABILITY_KEY = "mutability"


def _live() -> Any:
    """A field Nix reads at the point of use, so it can be changed at any time."""
    return Field(default=None, json_schema_extra={MUTABILITY_KEY: "live"})


def _construction() -> Any:
    """A field Nix reads once, while constructing the object that owns it.

    Changing it afterwards does nothing, so the setters refuse it rather than
    accept it silently.
    """
    return Field(default=None, json_schema_extra={MUTABILITY_KEY: "construction"})


def field_mutability(field: Any) -> Mutability:
    """Return when Nix reads ``field``. Untagged fields are ``"live"``.

    ``"live"`` is the default because it claims nothing: only the
    ``"construction"`` tag makes this library refuse a change, so an untagged
    field can never make a refusal appear out of a missing tag. A *missing*
    ``"construction"`` tag is the dangerous direction, and
    ``test_the_model_names_exactly_the_construction_time_eval_settings`` pins
    the whole set against that.
    """
    extras: dict[str, Any] = field.json_schema_extra or {}
    value: Any = extras.get(MUTABILITY_KEY)
    return "construction" if value == "construction" else "live"


def construction_time_keys(model: type[BaseModel]) -> frozenset[str]:
    """The ``nix.conf`` keys of the ``model`` fields Nix reads only at construction."""
    return frozenset(
        field_key(name, field)
        for name, field in model.model_fields.items()
        if field_mutability(field) == "construction"
    )


def reject_construction_time_keys(rendered: Mapping[str, str], *, model: type[BaseModel], target: str) -> None:
    """Raise if ``rendered`` names a setting Nix reads only at construction.

    The alternative is what this library used to do: send the setting, have Nix
    ignore it, and report success. A caller then believes an evaluator is pure
    when it is not.

    It checks the rendered keys rather than a typed model, because a rendered
    mapping is all that reaches a worker. Both engines check before they send,
    and ``CoreEvalState.configure`` checks again on the far side, so nothing
    can slip past by building a request by hand.

    Raises:
        SettingNotLiveError: One or more keys cannot take effect.
    """
    offenders = sorted(construction_time_keys(model) & set(rendered))
    if offenders:
        raise SettingNotLiveError(_not_live_message(offenders, target))


def reject_settings_write_while_open(*, open_stores: int, open_evaluators: int) -> None:
    """Raise if a store or an evaluator has already read the global settings.

    Nix builds a store and an evaluator from the globals as they stand at that
    moment, and neither looks again. Writing a global while one is open is
    therefore the silent-drop case in its purest form: the write succeeds, and
    the object that a caller is holding keeps the old value.

    Refusing is what makes ``set_settings`` honest without an allowlist of
    "settings that are safe to change late". Close what is open, write, then
    open again -- and everything opened after the write sees it.

    Raises:
        SettingNotLiveError: A store or an evaluator is open.
    """
    if not open_stores and not open_evaluators:
        return
    open_objects = ", ".join(
        f"{count} {noun}" for count, noun in ((open_stores, "store"), (open_evaluators, "evaluator")) if count
    )
    raise SettingNotLiveError(
        f"cannot change the Nix settings of this session while {open_objects} is open. "
        "Nix reads the settings while it constructs a store or an evaluator, and never again, "
        "so the change would not reach what you are holding. Close them first, or pass the "
        "settings to the session when you open it.",
    )


def _not_live_message(offenders: Sequence[str], target: str) -> str:
    names = ", ".join(offenders)
    plural = "settings" if len(offenders) > 1 else "setting"
    return (
        f"Nix reads {names} only while constructing the {target}, so changing it now does nothing. "
        f"Pass the {plural} when you open the {target} instead."
    )


def _nix_version_tuple(version: str) -> tuple[int, ...]:
    parsed: list[int] = []
    for part in version.split("."):
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        parsed.append(int(digits))
    return tuple(parsed)


def _nix_version_at_least(version: str, minimum: str) -> bool:
    actual = _nix_version_tuple(version)
    expected = _nix_version_tuple(minimum)
    width = max(len(actual), len(expected))
    return actual + (0,) * (width - len(actual)) >= expected + (0,) * (width - len(expected))


#: Metadata keys that bound a field to a range of Nix versions.
#: ``nix_version_min`` is the first version that has the setting, inclusive.
#: ``nix_version_removed`` is the first version that no longer has it,
#: exclusive -- a setting Nix dropped rather than one it never had.
NIX_VERSION_MIN_KEY = "nix_version_min"
NIX_VERSION_REMOVED_KEY = "nix_version_removed"

NIX_2_34 = "2.34"
NIX_2_35 = "2.35"


def since(version: str) -> Any:
    """A field the Nix versions before ``version`` do not have."""
    return Field(default=None, json_schema_extra={NIX_VERSION_MIN_KEY: version})


def until(version: str) -> Any:
    """A field Nix removed in ``version``."""
    return Field(default=None, json_schema_extra={NIX_VERSION_REMOVED_KEY: version})


def running_nix_version() -> str:
    """The version of the Nix that nanopynix is linked with."""
    build_info_result: Any = nanopynix_util.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
    return str(build_info_result["nix_version"])


def _field_version_problem(field: Any) -> str | None:
    """Why the running Nix cannot take this field, or ``None`` when it can."""
    extras: dict[str, Any] = field.json_schema_extra or {}
    minimum: Any = extras.get(NIX_VERSION_MIN_KEY)
    removed: Any = extras.get(NIX_VERSION_REMOVED_KEY)
    if minimum is None and removed is None:
        return None
    version = running_nix_version()
    if minimum is not None and not _nix_version_at_least(version, minimum):
        return f"requires Nix {minimum} or newer (running {version})"
    if removed is not None and _nix_version_at_least(version, removed):
        return f"was removed in Nix {removed} (running {version})"
    return None


def field_is_supported(field: Any) -> bool:
    return _field_version_problem(field) is None


class NixSettingMetadata(BaseModel):
    """Metadata exported by Nix for one registered setting."""

    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    value: Any = None
    default_value: Any = Field(default=None, alias="defaultValue")
    document_default: bool | None = Field(default=None, alias="documentDefault")
    experimental_feature: str | None = Field(default=None, alias="experimentalFeature")


class SettingsProvenance(BaseModel):
    """Where the effective Nix configuration of a session came from.

    Nix tracks an ``overridden`` flag for each setting, so the values a
    ``nix.conf`` supplied can be told apart from the values nanopynix applied
    afterwards. Without this a caller cannot tell an applied setting from one
    that was accepted and discarded.
    """

    #: What ``nix.conf``, ``NIX_CONFIG``, and the environment supplied, read
    #: immediately after ``initLibStore``. Empty when ``load_config`` is False.
    from_config: dict[str, str] = Field(default_factory=dict)
    #: What nanopynix applied afterwards, and Nix accepted.
    applied: dict[str, str] = Field(default_factory=dict)

    @property
    def overridden_from_config(self) -> dict[str, str]:
        """The settings nanopynix changed that ``nix.conf`` had also set.

        These are the ones worth showing a user: the host asked for one value
        and this session uses another.
        """
        return {name: value for name, value in self.applied.items() if name in self.from_config}


class SettingsDrift(BaseModel):
    """Difference between this Python model and Nix's live setting registry."""

    missing: list[str]
    extra: list[str]

    @property
    def ok(self) -> bool:
        """True if the model has neither missing nor extra settings."""
        return not self.missing and not self.extra


class NixConfigModel(BaseModel):
    """Shared base for Nix settings models: renders set fields as nix.conf key/value pairs."""

    model_config = ConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        extra="forbid",
    )

    def _iter_set(self, *, explicit_only: bool = False) -> Iterator[tuple[str, str]]:
        explicit = self.model_fields_set
        for name, field in type(self).model_fields.items():
            if explicit_only and name not in explicit:
                continue
            value = getattr(self, name)
            if value is None:
                continue
            problem = _field_version_problem(field)
            if problem is not None:
                raise ValueError(f"{field_key(name, field)} {problem}")
            rendered = _render_value(value)
            if rendered == "" and not isinstance(value, str):
                continue
            yield field_key(name, field), rendered

    def to_nix_config(self) -> str:
        """Render the set fields as ``nix.conf`` text (``key = value`` lines)."""
        return "\n".join(f"{key} = {value}" for key, value in self._iter_set())

    def to_worker_settings(self, *, explicit_only: bool = False) -> dict[str, str]:
        """Render the set fields as a ``{nix-conf-key: rendered-value}`` mapping.

        Args:
            explicit_only: Render only the fields the caller actually named,
                leaving out every default. A *write* to an already-configured
                session needs this: ``experimental_features`` has a default, so
                without it ``set_settings(NixGlobalSettings(max_jobs=3))`` would
                also reset the feature list and silently drop whatever that
                session had enabled beyond the defaults.
        """
        return dict(self._iter_set(explicit_only=explicit_only))

    @classmethod
    def for_scope(cls, settings: BaseModel) -> Self:
        """Take this scope's fields out of a wider settings object.

        :class:`NixSettings` inherits every scope, but Nix keeps each in its own
        registry and reads each at its own moment. This is what separates them
        again, so a session can send the globals to ``globalConfig`` and hold
        the rest as defaults for the objects that read them.

        Both engines do this to the same object and must get the same answer,
        so the extraction lives here rather than in each session.
        """
        return cls.model_validate(settings.model_dump(include=set(cls.model_fields), exclude_none=True))


def fill_unset_fields[M: NixConfigModel](spec: M, defaults: NixConfigModel) -> M:
    """Return ``spec`` with each field it left unset filled from ``defaults``.

    The one place "a value set on the object itself wins" is written down. A
    field counts as unset when it is ``None``, which is exact for every scope
    model here: each of their fields defaults to ``None``.

    ``defaults`` is any settings model, not necessarily ``spec``'s own class,
    because the two are related by field name rather than by inheritance. A
    store model takes its defaults from :class:`NixStoreDefaults`, which is not
    a store type; a name ``spec`` does not have is skipped.
    """
    fields = type(spec).model_fields
    filled = {
        name: value
        for name, value in defaults.model_dump(exclude_none=True).items()
        if name in fields and getattr(spec, name) is None
    }
    return spec.model_copy(update=filled) if filled else spec


def merge_defaults[M: NixConfigModel](spec: M | None, defaults: M) -> M:
    """Merge a per-call settings object over this session's defaults for that scope.

    What makes a session-wide default real: a caller states ``pure_eval`` once
    on the session, and every evaluator it opens is pure, unless that call says
    otherwise. ``spec`` of ``None`` means the call said nothing at all, so the
    defaults apply whole.
    """
    return defaults if spec is None else fill_unset_fields(spec, defaults)


class NixGlobalSettings(NixConfigModel):
    """The process-global Nix settings, as registered in ``globalConfig``.

    One scope of several. :class:`NixSettings` inherits this together with the
    store, eval, fetch and flake scopes, and is what a caller normally passes.
    This class exists on its own because ``globalConfig`` is a distinct registry,
    and :func:`check_settings_model_drift` compares against it field for field.

    ``store`` here is the *URL of the store to use*, which is what
    ``globals.hh`` registers under that name. It is not the logical store
    directory, which Nix also calls ``store`` but registers per store type --
    see :mod:`nanopynix.stores`, where it is spelled ``store_dir``.
    """

    allow_new_privileges: bool | None = None
    allow_symlinked_store: bool | None = None
    allowed_impure_host_deps: list[str] | None = None
    always_allow_substitutes: bool | None = None
    auto_allocate_uids: bool | None = None
    auto_optimise_store: bool | None = None
    build_dir: str | None = None
    build_hook: str | None = None
    build_poll_interval: int | None = None
    build_users_group: str | None = None
    builders: list[str] | None = None
    builders_use_substitutes: bool | None = None
    compress_build_log: bool | None = None
    connect_timeout: int | None = None
    cores: int | None = None
    diff_hook: str | None = None
    download_attempts: int | None = None
    download_buffer_size: int | None = None
    download_speed: int | None = None
    experimental_features: list[str] | None = Field(default_factory=lambda: list(DEFAULT_EXPERIMENTAL_FEATURES))
    external_builders: list[str] | None = None
    extra_platforms: list[str] | None = None
    fallback: bool | None = None
    filetransfer_retry_attempts: int | None = since(NIX_2_35)
    filetransfer_retry_delay: int | None = since(NIX_2_35)
    filetransfer_retry_delay_rate_limited: int | None = since(NIX_2_35)
    filetransfer_retry_jitter: bool | None = since(NIX_2_35)
    filetransfer_retry_max_delay: int | None = since(NIX_2_35)
    filter_syscalls: bool | None = None
    fsync_metadata: bool | None = None
    fsync_store_paths: bool | None = None
    gc_reserved_space: int | None = None
    hashed_mirrors: list[str] | None = None
    http2: bool | None = None
    http3: bool | None = since(NIX_2_35)
    http_connections: int | None = None
    id_count: int | None = None
    ignored_acls: list[str] | None = None
    impersonate_linux_26: bool | None = None
    impure_env: list[str] | None = None
    json_log_path: str | None = None
    keep_build_log: bool | None = None
    keep_derivations: bool | None = None
    keep_failed: bool | None = None
    keep_going: bool | None = None
    keep_outputs: bool | None = None
    log_lines: int | None = None
    max_build_log_size: int | None = None
    max_free: int | None = None
    max_jobs: int | None = None
    max_silent_time: int | None = None
    max_substitution_jobs: int | None = None
    min_free: int | None = None
    min_free_check_interval: int | None = None
    nar_buffer_size: int | None = None
    narinfo_cache_meta_ttl: int | None = None
    narinfo_cache_negative_ttl: int | None = None
    narinfo_cache_positive_ttl: int | None = None
    netrc_file: str | None = None
    # Three settings are deliberately absent from this block, all of them
    # registered by Nix components nanopynix stopped linking, and all three
    # meaningless to a library even if they were registered.
    # `check_settings_model_drift()` is what notices, which is what it is for.
    #
    # `nix-shell-always-looks-for-shell-nix` and
    # `nix-shell-shebang-arguments-relative-to-script`: libcmd's
    # CompatibilitySettings. Both describe how the `nix-shell` *command*
    # interprets its arguments, and a library binding has no `nix-shell`.
    #
    # `plugin-files`: libmain's PluginSettings. It names C++ DSOs for
    # `initPlugins` to dlopen into the process, which must be ABI-compatible
    # with the exact Nix build; nanopynix does not bind `initPlugins`, and
    # `register_primop` is how you extend this evaluator.
    post_build_hook: str | None = None
    pre_build_hook: str | None = None
    preallocate_contents: bool | None = None
    print_missing: bool | None = None
    require_drop_supplementary_groups: bool | None = None
    require_sigs: bool | None = None
    run_diff_hook: bool | None = None
    sandbox: str | None = None
    sandbox_build_dir: str | None = None
    sandbox_dev_shm_size: str | None = None
    sandbox_fallback: bool | None = None
    sandbox_paths: list[str] | None = None
    secret_key_files: list[str] | None = None
    show_trace: bool | None = None
    ssl_cert_file: str | None = None
    stalled_download_timeout: int | None = None
    start_id: int | None = None
    store: str | None = None
    substitute: bool | None = None
    substituters: list[str] | None = None
    sync_before_registering: bool | None = None
    system: str | None = None
    system_features: list[str] | None = None
    timeout: int | None = None
    trusted_public_keys: list[str] | None = None
    trusted_substituters: list[str] | None = None
    use_case_hack: bool | None = None
    use_cgroups: bool | None = None
    use_sqlite_wal: bool | None = None
    use_xdg_base_directories: bool | None = None
    user_agent_suffix: str | None = None
    warn_large_path_threshold: int | None = None

    @classmethod
    def from_file(cls, path: os.PathLike[str] | str) -> Self:
        """Load settings from a ``.json``, ``.toml``, ``.yaml``/``.yml``, or ``nix.conf``-style file.

        Format is inferred from the file extension; anything else is parsed
        as ``nix.conf`` (``key = value`` lines, ``#`` comments).
        """
        config_path = Path(os.fspath(path))
        text = config_path.read_text()
        raw: object
        if config_path.suffix == ".json":
            raw = json.loads(text)
        elif config_path.suffix == ".toml":
            raw = tomllib.loads(text)
        elif config_path.suffix in {".yaml", ".yml"}:
            raw = yaml.safe_load(text) or {}
        else:
            raw = _parse_nix_conf(text)
        if not isinstance(raw, dict):
            raise TypeError(f"Nix settings file must contain an object: {config_path}")
        return cls.model_validate(raw)

    def with_experimental_features(self, features: list[str] | None) -> Self:
        """Return a copy with ``features`` merged into ``experimental_features`` (order-preserving, deduplicated)."""
        if not features:
            return self
        merged = [*(self.experimental_features or [])]
        for feature in features:
            if feature not in merged:
                merged.append(feature)
        return self.model_copy(update={"experimental_features": merged})


class NixStoreDefaults(NixConfigModel):
    """Store settings that make sense as a session-wide default.

    Exactly the settings every one of Nix's 10 store types accepts *and* that
    ``globalConfig`` does not register. Nix has no global for these, so a
    default is only meaningful because nanopynix renders it into each store's
    URI itself -- which is also the only spelling that was measured to work.

    The fifth and sixth settings shared by all store types are deliberately
    absent. ``system-features`` is in ``globalConfig`` already, so it lives on
    :class:`NixGlobalSettings`. ``store`` -- the *logical store directory* --
    is per store and never a session-wide default: it is the setting two
    stores must agree on before a path can be copied between them, so it
    belongs on the store, spelled ``store_dir`` in :mod:`nanopynix.stores`.
    """

    path_info_cache_size: int | None = None
    priority: int | None = None
    trusted: bool | None = None
    want_mass_query: bool | None = None


class NixEvalSettings(NixConfigModel):
    """Eval-specific Nix settings, applied per-``EvalState``.

    Each field records **when Nix reads it**, because that decides whether it
    can be changed on an evaluator that already exists. Eight of the settings
    below are read once, while ``EvalState`` is being constructed; the rest are
    read at the point of use, so they can be changed at any time.

    :meth:`EvalSession.configure` refuses the construction-time ones instead of
    accepting them and doing nothing, which is what it used to do. To change
    one, open a new evaluator: ``session.eval(store, eval_settings=...)``.

    The classification comes from mapping every ``EvalSettings`` member to its
    use sites in Nix 2.34 ``src/libexpr/``, then checking the behaviour against
    a running Nix. Two results were not obvious from the header:

    * ``restrict_eval`` is also read live, in ``checkURI``. It is still tagged
      construction, because path restriction rides the source accessor chosen
      in the constructor -- measured: ``configure(restrict_eval=True)`` did not
      stop ``readFile`` of a path outside the store.
    * ``eval_system`` backs ``builtins.currentSystem``, which
      ``createBaseEnv`` registers with ``addConstant`` while constructing the
      evaluator.
    """

    allow_import_from_derivation: bool | None = _live()
    allow_unsafe_native_code_during_evaluation: bool | None = _live()
    allowed_uris: list[str] | None = _live()
    abort_on_warn: bool | None = _live()
    debugger_on_trace: bool | None = _live()
    debugger_on_warn: bool | None = _live()
    eval_attrset_update_layer_rhs_threshold: int | None = _live()
    eval_cache: bool | None = _live()
    eval_profile_file: str | None = _construction()
    eval_profiler: str | None = _construction()
    eval_profiler_frequency: int | None = _construction()
    eval_system: str | None = _construction()
    ignore_try: bool | None = _live()
    lint_absolute_path_literals: str | None = _live()
    lint_short_path_literals: str | None = _live()
    lint_url_literals: str | None = _live()
    max_call_depth: int | None = _live()
    nix_path: list[str] | None = _construction()
    pure_eval: bool | None = _construction()
    restrict_eval: bool | None = _construction()
    trace_function_calls: bool | None = _construction()
    trace_import_from_derivation: bool | None = _live()
    trace_verbose: bool | None = _live()
    #: Deprecated by Nix in favour of ``lint_short_path_literals = "warn"``;
    #: Nix registers it as a ``DeprecatedWarnSetting`` over the same value.
    warn_short_path_literals: bool | None = _live()


NIX_PATH_SETTING_KEY = _alias("nix_path")
"""The rendered nix.conf key for NixEvalSettings.nix_path -- derived from the
same alias_generator that renders it, rather than restated as a literal."""


class NixFetchSettings(NixConfigModel):
    """Fetcher-specific Nix settings, applied per-evaluator.

    Every field is live. ``EvalState`` holds ``const fetchers::Settings &`` -- a
    reference, documented "Must outlive the lifetime of this EvalState!" -- and
    nothing copies it at construction, so there is no snapshot to go stale.
    :meth:`EvalSession.configure` therefore accepts all of these.
    """

    access_tokens: dict[str, str] | None = _live()
    allow_dirty: bool | None = _live()
    allow_dirty_locks: bool | None = _live()
    flake_registry: str | None = _live()
    # Nix 2.31 keeps `tarball-ttl` in the global registry. Nix 2.34 moved it
    # into the fetcher registry, where the two versions disagree about the
    # owner rather than about the setting. Live, like every field here -- the
    # version gate is what it has to say, and untagged already means live.
    tarball_ttl: int | None = since(NIX_2_34)
    trust_tarballs_from_git_forges: bool | None = _live()
    warn_dirty: bool | None = _live()


class NixFlakeSettings(NixConfigModel):
    """Flake-specific Nix settings, applied per flake operation."""

    accept_flake_config: bool | None = None
    commit_lock_file_summary: str | None = None
    use_registries: bool | None = None


class NixEvaluatorSettings(NixEvalSettings, NixFetchSettings):
    """Everything one evaluator is configured with, in one object.

    Nix keeps the eval and fetch settings in two registries, and this library
    keeps two classes so each can be checked against its own registry. An
    evaluator owns both, so this is what :meth:`Session.eval` takes. The two
    registries share no setting name, so nothing is ambiguous here.

    A narrow parameter typed :class:`NixEvalSettings` accepts one of these,
    because it really is one. :func:`split_evaluator_settings` is what makes
    that true at run time: it sends each half to the registry that owns it. The
    parameter used to render the whole object into one map, and Nix answered
    ``unknown eval setting: warn-dirty``.
    """


class NixSettings(NixGlobalSettings, NixStoreDefaults, NixEvaluatorSettings, NixFlakeSettings):
    """Every Nix setting nanopynix can express, in one object.

    The catch-all: hand it to a :class:`Session` and each field reaches the
    place that can honour it. The globals are applied to the process, and the
    store, eval, fetch and flake fields become that session's *defaults* --
    overridden by anything passed to an individual store or evaluator.

    Each scope is also its own class, so a narrower parameter can ask for
    exactly what it uses and still accept this. The scopes exist because Nix
    reads them at different moments, and :func:`check_settings_model_drift`
    compares each against its own registry.

    One Nix setting is deliberately unreachable from here. Nix registers
    ``store`` twice, meaning the store *URL* globally and the *logical store
    directory* per store. Only the first is a session-wide idea, so it is the
    one this class inherits; for the second, see ``store_dir`` in
    :mod:`nanopynix.stores`.
    """


class NixSettingsEnv(NixSettings, BaseSettings):
    """Environment-backed Nix settings for command-line tools."""

    model_config = SettingsConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        env_prefix="PYNIX_NIX_",
        env_nested_delimiter="__",
        extra="forbid",
    )


#: Which scope owns each field, and where a caller must send it. Built from the
#: five scope classes, so a field added to one of them is routed by that fact
#: alone. The order matters only for the message: the first scope that claims a
#: name wins, and ``test_the_four_settings_registries_are_disjoint`` is what
#: keeps a name from being claimed twice.
_SCOPE_DOORS: tuple[tuple[type[NixConfigModel], str], ...] = (
    (NixGlobalSettings, "a global setting; pass it to Session(settings=...)"),
    (NixStoreDefaults, "a store setting; pass it to the store model, or to Session(settings=...)"),
    (NixEvalSettings, "an eval setting; pass it as eval_settings"),
    (NixFetchSettings, "a fetch setting; pass it as fetch_settings"),
    (NixFlakeSettings, "a flake setting; pass it as flake_settings"),
)


def _door_of(field_name: str) -> str:
    for scope, door in _SCOPE_DOORS:
        if field_name in scope.model_fields:
            return door
    return "not a setting this library routes"


def reject_out_of_scope_settings(
    settings: NixConfigModel | None,
    *,
    scopes: Sequence[type[NixConfigModel]],
    target: str,
) -> None:
    """Raise if ``settings`` names a field that none of ``scopes`` owns.

    A parameter is typed for the scope it writes, and a wider model is still an
    instance of that type. ``NixSettings`` is a ``NixEvalSettings``, so
    ``eval_settings`` accepts one, and its global fields reach no evaluator.
    Nix used to answer for this, and it answered badly: the message named
    ``experimental-features``, a field the caller never set, because
    ``NixSettings`` gives that one a default.

    It reads ``model_fields_set`` rather than the non-``None`` fields, so a
    default never trips it. Call it *before* any merge with the session
    defaults: :func:`fill_unset_fields` copies with ``model_copy(update=...)``,
    and that marks a filled default as set.

    Raises:
        SettingOutOfScopeError: One or more fields belong to another door.
    """
    if settings is None:
        return
    owned = {name for scope in scopes for name in scope.model_fields}
    strays = sorted(settings.model_fields_set - owned)
    if not strays:
        return
    fields = type(settings).model_fields
    detail = "; ".join(f"{field_key(name, fields[name])} is {_door_of(name)}" for name in strays)
    raise SettingOutOfScopeError(f"cannot apply these settings to {target}: {detail}")


def render_for_scope(
    settings: NixConfigModel,
    scope: type[NixConfigModel],
    *,
    explicit_only: bool = False,
) -> dict[str, str]:
    """Render ``settings``, keeping only the keys ``scope`` owns.

    It filters the *rendered mapping* rather than narrowing the model, because
    :meth:`NixConfigModel.for_scope` re-validates and so marks a default as
    explicitly set. That would defeat ``explicit_only``, whose whole purpose is
    to write one field without resetting the others.
    """
    keys = {field_key(name, field) for name, field in scope.model_fields.items()}
    return {
        key: value for key, value in settings.to_worker_settings(explicit_only=explicit_only).items() if key in keys
    }


def narrow_to_scope[M: NixConfigModel](
    settings: NixConfigModel | None,
    scope: type[M],
    *,
    target: str,
) -> M | None:
    """Refuse a field ``scope`` does not own, then keep only that scope.

    For a door that writes exactly one registry. The evaluator writes two and
    has to re-route between them, which is :func:`split_evaluator_settings`.

    Raises:
        SettingOutOfScopeError: A field belongs to another door.
    """
    reject_out_of_scope_settings(settings, scopes=(scope,), target=target)
    return scope.for_scope(settings) if settings is not None else None


def split_evaluator_settings(
    eval_settings: NixEvalSettings | None,
    fetch_settings: NixFetchSettings | None,
) -> tuple[NixEvalSettings | None, NixFetchSettings | None]:
    """Route each half of an evaluator's settings to the scope that owns it.

    An evaluator owns two of Nix's registries, and :class:`NixEvaluatorSettings`
    states both in one object. Either parameter accepts one, because it really
    is one of each. Without this, the whole object went to a single door and
    Nix answered ``unknown eval setting: warn-dirty``.

    The two scopes share no field name, so splitting one object is never
    ambiguous. Where both *parameters* name the same field, the one that owns
    that scope wins: a fetch field on ``fetch_settings`` beats the same field
    carried along by ``eval_settings``.

    Raises:
        SettingOutOfScopeError: A field belongs to neither evaluator scope.
    """
    evaluator_scopes = (NixEvalSettings, NixFetchSettings)
    reject_out_of_scope_settings(eval_settings, scopes=evaluator_scopes, target="an evaluator")
    reject_out_of_scope_settings(fetch_settings, scopes=evaluator_scopes, target="an evaluator")
    if eval_settings is None and fetch_settings is None:
        return None, None
    eval_part = fill_unset_fields(
        NixEvalSettings.for_scope(eval_settings) if eval_settings is not None else NixEvalSettings(),
        NixEvalSettings.for_scope(fetch_settings) if fetch_settings is not None else NixEvalSettings(),
    )
    fetch_part = fill_unset_fields(
        NixFetchSettings.for_scope(fetch_settings) if fetch_settings is not None else NixFetchSettings(),
        NixFetchSettings.for_scope(eval_settings) if eval_settings is not None else NixFetchSettings(),
    )
    return eval_part, fetch_part


DEFAULT_RPC_TIMEOUT_SECONDS = 300.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
DEFAULT_WORKER_MAX_CONCURRENCY = 32
"""Shared between the worker's own gRPC server (rpc.worker._worker) and the
client's dispatch limiter (rpc.client._pool) -- the client must not exceed
what the worker's transport is configured to serve concurrently."""

DEFAULT_WORKER_PRELOAD = ("nanopynix.rpc.worker._worker",)
"""The modules the forkserver imports once, before it forks any worker.

One import of nanopynix, its bindings and grpclib, shared by every worker of
the process through copy-on-write. Without it each worker pays that import
itself.

**This does not decide which build of the extension a worker gets, and no
setting can.** The forkserver child imports this module while it unpickles the
process target, before any code of ours runs there, and ``multiprocessing``
keeps one forkserver for each process, started from the parent's ``sys.path``.
So the venv decides, once, and a build with no Boehm collector is a different
venv. See ``nanopynixVersions`` in ``default.nix``."""


class NanopynixSettings(BaseSettings):
    """Runtime settings for nanopynix itself, never forwarded to Nix."""

    model_config = SettingsConfigDict(env_prefix="NANOPYNIX_", extra="forbid")

    rpc_timeout: float = Field(default=DEFAULT_RPC_TIMEOUT_SECONDS, gt=0)
    shutdown_timeout: float = Field(default=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, gt=0)
    line_editors: list[str] = Field(default_factory=lambda: list(DEFAULT_LINE_EDITORS))
    worker_preload: list[str] = Field(default_factory=lambda: list(DEFAULT_WORKER_PRELOAD))
    """Which modules the forkserver preloads. An empty list preloads nothing.

    A setting rather than the constant it used to be, because the forkserver
    is process-global and the list was written into a library. Two consumers
    in one process cannot ask for different lists, and the first one to open a
    session silently decides for both. Naming it makes that visible, and gives
    a consumer with a different module layout, or one that wants the
    instrumented extension kept out of the forkserver, somewhere to say so.

    See :data:`DEFAULT_WORKER_PRELOAD` for what it costs to empty it."""


@no_runtime_type_check  # settings validates its own type at runtime for untyped
# callers (see the isinstance guards below); beartype's parameter check would
# otherwise intercept before those guards run and raise its own exception
# type instead of the documented TypeError.
def normalize_nix_settings(settings: NixSettings | os.PathLike[str] | str | None) -> NixSettings:
    """Coerce ``settings`` to a ``NixSettings`` instance.

    ``None`` becomes defaults; a path-like value is loaded with
    :meth:`NixSettings.from_file`; an existing ``NixSettings`` passes through.
    """
    if settings is None:
        return NixSettings()
    if isinstance(settings, NixSettings):
        return settings
    if isinstance(settings, str | os.PathLike):  # type: ignore[reportUnnecessaryIsInstance] -- runtime guard for untyped callers
        return NixSettings.from_file(settings)
    raise TypeError("settings must be a NixSettings instance, an anyio.Path/path-like config file, or None")


def normalize_nix_path(nix_path: str | Sequence[str] | None) -> list[str]:
    """Coerce ``nix_path`` to a resolved list of ``NIX_PATH`` entries.

    ``None`` falls back to parsing the process environment's ``NIX_PATH``; a
    colon-separated string is parsed the same way Nix itself would.
    """
    if nix_path is None:
        return list(nanopynix_expr.parse_nix_path())
    if isinstance(nix_path, str):
        return list(nanopynix_expr.parse_nix_path(nix_path))
    return list(nix_path)


def list_settings_metadata() -> dict[str, NixSettingMetadata]:
    """Return Nix's live registry of global store/eval settings, keyed by name."""
    raw: dict[str, object] = json.loads(nanopynix_util.list_settings_metadata_json())
    return _settings_metadata_from_raw(raw)


def list_eval_settings_metadata() -> dict[str, NixSettingMetadata]:
    """Return Nix's live registry of evaluator-specific settings, keyed by name."""
    raw: dict[str, object] = json.loads(nanopynix_expr.list_eval_settings_metadata_json())
    return _settings_metadata_from_raw(raw)


def list_fetch_settings_metadata() -> dict[str, NixSettingMetadata]:
    """Return Nix's live registry of fetcher-specific settings, keyed by name."""
    raw: dict[str, object] = json.loads(nanopynix_fetchers.list_fetch_settings_metadata_json())
    return _settings_metadata_from_raw(raw)


def list_flake_settings_metadata() -> dict[str, NixSettingMetadata]:
    """Return Nix's live registry of flake-specific settings, keyed by name."""
    raw: dict[str, object] = json.loads(nanopynix_flake.list_flake_settings_metadata_json())
    return _settings_metadata_from_raw(raw)


def check_settings_model_drift(
    metadata: Mapping[str, NixSettingMetadata] | None = None,
    *,
    surface: SettingsSurface = "global",
) -> SettingsDrift:
    """Compare the Pydantic settings model for ``surface`` against Nix's live registry.

    Args:
        metadata: Registry to compare against; defaults to the live registry
            for ``surface`` (via e.g. :func:`list_settings_metadata`).
        surface: Which settings model/registry pair to compare.

    Returns:
        Settings registered in Nix but missing from the model, and settings
        in the model but not registered in Nix (excluding known aliases).
    """
    if metadata is None:
        metadata = _metadata_for_surface(surface)
    # The global surface used to subtract the eval, fetch and flake names here,
    # on the belief that `globalConfig` aggregates those registries. It does
    # not: the four are disjoint, measured on every supported version --
    # 2.31.5, 2.34.8 and 2.35.1 each report zero overlap between `globalConfig`
    # and any of the other three. The step removed nothing, and
    # `test_the_four_settings_registries_are_disjoint` now pins that.
    known = set(metadata.keys())
    model_type = _model_for_surface(surface)
    model = {field_key(name, field) for name, field in model_type.model_fields.items() if field_is_supported(field)}
    registered_aliases = {alias for setting in metadata.values() for alias in setting.aliases}
    return SettingsDrift(missing=sorted(known - model), extra=sorted(model - known - registered_aliases))


def check_all_settings_model_drift(*, include_optional: bool = False) -> dict[str, SettingsDrift]:
    """Run :func:`check_settings_model_drift` across settings surfaces, keyed by surface name.

    Args:
        include_optional: If True, also check the ``eval``, ``fetch``, and
            ``flake`` surfaces in addition to ``global``.
    """
    surfaces: tuple[SettingsSurface, ...] = ("global", "eval", "fetch", "flake") if include_optional else ("global",)
    return {surface: check_settings_model_drift(surface=surface) for surface in surfaces}


def _settings_metadata_from_raw(raw: object) -> dict[str, NixSettingMetadata]:
    if not isinstance(raw, dict):
        raise TypeError("Nix returned non-object settings metadata")
    typed_raw = cast("dict[str, object]", raw)
    return {key: NixSettingMetadata.model_validate(value) for key, value in typed_raw.items()}


def _metadata_for_surface(surface: SettingsSurface) -> dict[str, NixSettingMetadata]:
    if surface == "global":
        return list_settings_metadata()
    if surface == "eval":
        return list_eval_settings_metadata()
    if surface == "fetch":
        return list_fetch_settings_metadata()
    if surface == "flake":
        return list_flake_settings_metadata()
    raise ValueError(f"unknown settings surface: {surface}")


def _model_for_surface(surface: SettingsSurface) -> type[BaseModel]:
    # The scope class, never the NixSettings catch-all. Each surface is checked
    # against its own registry, and the catch-all inherits every scope, so it
    # would report all the other scopes' fields as `extra` on each one.
    if surface == "global":
        return NixGlobalSettings
    if surface == "eval":
        return NixEvalSettings
    if surface == "fetch":
        return NixFetchSettings
    if surface == "flake":
        return NixFlakeSettings
    raise ValueError(f"unknown settings surface: {surface}")


def _parse_nix_conf(text: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if sep == "":
            raise ValueError(f"invalid nix.conf setting line: {line!r}")
        raw[key.strip()] = value.strip()
    return raw


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        typed_value = cast("dict[object, object]", value)
        return " ".join(f"{key}={item}" for key, item in typed_value.items())
    if isinstance(value, list):
        typed_value = cast("list[object]", value)
        return " ".join(str(item) for item in typed_value)
    return str(value)
