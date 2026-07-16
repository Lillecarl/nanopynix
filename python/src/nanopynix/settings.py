"""Typed Nix configuration models."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import nanopynix_expr
import nanopynix_fetchers
import nanopynix_flake
import nanopynix_util

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


DEFAULT_EXPERIMENTAL_FEATURES = ("flakes", "nix-command")
DEFAULT_LINE_EDITORS = ("emacs", "nano", "vim", "kak", "hx")
type SettingsSurface = Literal["global", "eval", "fetch", "flake"]


def _alias(field_name: str) -> str:
    return field_name.replace("_", "-")


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


def _field_is_supported(field: Any) -> bool:
    extras: dict[str, Any] = field.json_schema_extra or {}
    minimum: Any = extras.get("nix_version_min")
    if minimum is None:
        return True
    build_info_result: Any = nanopynix_util.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
    return _nix_version_at_least(build_info_result["nix_version"], minimum)


class NixSettingMetadata(BaseModel):
    """Metadata exported by Nix for one registered setting."""

    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    value: Any = None
    default_value: Any = Field(default=None, alias="defaultValue")
    document_default: bool | None = Field(default=None, alias="documentDefault")
    experimental_feature: str | None = Field(default=None, alias="experimentalFeature")


class SettingsDrift(BaseModel):
    """Difference between this Python model and Nix's live setting registry."""

    missing: list[str]
    extra: list[str]

    @property
    def ok(self) -> bool:
        """True if the model has neither missing nor extra settings."""
        return not self.missing and not self.extra


class NixSettings(BaseModel):
    """Nix settings rendered as nix.conf-compatible key/value pairs."""

    model_config = ConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        extra="forbid",
    )

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
    filetransfer_retry_attempts: int | None = Field(default=None, json_schema_extra={"nix_version_min": "2.35"})
    filetransfer_retry_delay: int | None = Field(default=None, json_schema_extra={"nix_version_min": "2.35"})
    filetransfer_retry_delay_rate_limited: int | None = Field(
        default=None, json_schema_extra={"nix_version_min": "2.35"}
    )
    filetransfer_retry_jitter: bool | None = Field(default=None, json_schema_extra={"nix_version_min": "2.35"})
    filetransfer_retry_max_delay: int | None = Field(default=None, json_schema_extra={"nix_version_min": "2.35"})
    filter_syscalls: bool | None = None
    fsync_metadata: bool | None = None
    fsync_store_paths: bool | None = None
    gc_reserved_space: int | None = None
    hashed_mirrors: list[str] | None = None
    http2: bool | None = None
    http3: bool | None = Field(default=None, json_schema_extra={"nix_version_min": "2.35"})
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
    nix_shell_always_looks_for_shell_nix: bool | None = Field(
        default=None, json_schema_extra={"nix_version_min": "2.24"}
    )
    nix_shell_shebang_arguments_relative_to_script: bool | None = Field(
        default=None, json_schema_extra={"nix_version_min": "2.24"}
    )
    plugin_files: list[str] | None = None
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
    def from_file(cls, path: os.PathLike[str] | str) -> NixSettings:
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

    def with_experimental_features(self, features: list[str] | None) -> NixSettings:
        """Return a copy with ``features`` merged into ``experimental_features`` (order-preserving, deduplicated)."""
        if not features:
            return self
        merged = [*(self.experimental_features or [])]
        for feature in features:
            if feature not in merged:
                merged.append(feature)
        return self.model_copy(update={"experimental_features": merged})

    def _iter_set(self) -> Iterator[tuple[str, str]]:
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            if value is None:
                continue
            if not _field_is_supported(field):
                extra: Any = field.json_schema_extra
                minimum = extra["nix_version_min"]
                build_info_result: Any = nanopynix_util.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- C++ extension without type stubs
                version = build_info_result["nix_version"]
                raise ValueError(f"{_alias(name)} requires Nix {minimum} or newer (running {version})")
            rendered = _render_value(value)
            if rendered == "" and not isinstance(value, str):
                continue
            yield _alias(name), rendered

    def to_nix_config(self) -> str:
        """Render the set fields as ``nix.conf`` text (``key = value`` lines)."""
        return "\n".join(f"{key} = {value}" for key, value in self._iter_set())

    def to_worker_settings(self) -> dict[str, str]:
        """Render the set fields as a ``{nix-conf-key: rendered-value}`` mapping."""
        return dict(self._iter_set())


class NixEvalSettings(BaseModel):
    """Eval-specific Nix settings not applied through the global store settings path."""

    model_config = ConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        extra="forbid",
    )

    allow_import_from_derivation: bool | None = None
    allow_unsafe_native_code_during_evaluation: bool | None = None
    allowed_uris: list[str] | None = None
    abort_on_warn: bool | None = None
    debugger_on_trace: bool | None = None
    debugger_on_warn: bool | None = None
    eval_attrset_update_layer_rhs_threshold: int | None = None
    eval_cache: bool | None = None
    eval_profile_file: str | None = None
    eval_profiler: str | None = None
    eval_profiler_frequency: int | None = None
    eval_system: str | None = None
    ignore_try: bool | None = None
    lint_absolute_path_literals: str | None = None
    lint_short_path_literals: str | None = None
    lint_url_literals: str | None = None
    max_call_depth: int | None = None
    nix_path: list[str] | None = None
    pure_eval: bool | None = None
    restrict_eval: bool | None = None
    trace_function_calls: bool | None = None
    trace_import_from_derivation: bool | None = None
    trace_verbose: bool | None = None
    warn_short_path_literals: bool | None = None


class NixFetchSettings(BaseModel):
    """Fetcher-specific Nix settings."""

    model_config = ConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        extra="forbid",
    )

    access_tokens: dict[str, str] | None = None
    allow_dirty: bool | None = None
    allow_dirty_locks: bool | None = None
    flake_registry: str | None = None
    tarball_ttl: int | None = None
    trust_tarballs_from_git_forges: bool | None = None
    warn_dirty: bool | None = None


class NixFlakeSettings(BaseModel):
    """Flake-specific Nix settings."""

    model_config = ConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        extra="forbid",
    )

    accept_flake_config: bool | None = None
    commit_lock_file_summary: str | None = None
    use_registries: bool | None = None


class NixSettingsEnv(NixSettings, BaseSettings):
    """Environment-backed Nix settings for command-line tools."""

    model_config = SettingsConfigDict(
        alias_generator=_alias,
        populate_by_name=True,
        env_prefix="PYNIX_NIX_",
        env_nested_delimiter="__",
        extra="forbid",
    )


class NanopynixSettings(BaseSettings):
    """Runtime settings for nanopynix itself, never forwarded to Nix."""

    model_config = SettingsConfigDict(env_prefix="NANOPYNIX_", extra="forbid")

    rpc_timeout: float = Field(default=300.0, gt=0)
    shutdown_timeout: float = Field(default=5.0, gt=0)
    line_editors: list[str] = Field(default_factory=lambda: list(DEFAULT_LINE_EDITORS))


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
    if surface == "global":
        # globalConfig aggregates every registered Config instance, including
        # the evaluator, fetcher, and flake-specific settings.  Those have
        # dedicated Python models and must not be reported as missing from
        # NixSettings.
        non_global_settings = set(list_eval_settings_metadata())
        non_global_settings.update(list_fetch_settings_metadata())
        non_global_settings.update(list_flake_settings_metadata())
        metadata = {name: setting for name, setting in metadata.items() if name not in non_global_settings}
    known = set(metadata.keys())
    model_type = _model_for_surface(surface)
    model = {
        _alias(name)
        for name, field in model_type.model_fields.items()
        if _field_is_supported(field)
    }
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
    if surface == "global":
        return NixSettings
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
