# ruff: noqa: F401
# pyright: reportUnusedImport=false
"""nanopynix — nanobind-based Python bindings for Nix."""

from __future__ import annotations

from nanopynix_proto.nix.common import LogLevel
from strip_ansi import strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

from nanopynix._pool import WorkerBusyError, WorkerDiedError
from nanopynix._session import EvalSession, LockedFlakeHandle, ValueAttrs, ValueList, ValueProxy
from nanopynix.exceptions import (
    EvalError,
    EvalProxyError,
    EvalSessionClosedError,
    ForeignValueError,
    InfiniteRecursionError,
    MissingArgumentError,
    NixAssertionError,
    NixCoercionError,
    NixError,
    NixTypeError,
    ParseError,
    RestrictedPathError,
    StoreError,
    ThrownError,
    UndefinedVarError,
    UnresolvedValueError,
    UsageError,
    ValueReleasedError,
    WrongNixTypeError,
)
from nanopynix.logging import LogCollector
from nanopynix.models import (
    BuildResult,
    Derivation,
    DerivationOutputs,
    FlakeRef,
    Input,
    LockedFlake,
    LockedInput,
    LogEvent,
    MissingInfo,
    NixType,
    PathInfo,
    PrimOpSpec,
    ResultType,
    StorePath,
    ValueHandle,
)
from nanopynix.nix import LogCapture, Nix, Session
from nanopynix.primops import from_yaml, from_yaml11, from_yaml11_stream, from_yaml_stream, to_yaml, yaml_primops
from nanopynix.settings import (
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
    NixSettingMetadata,
    NixSettings,
    NixSettingsEnv,
    SettingsDrift,
    check_all_settings_model_drift,
    check_settings_model_drift,
    list_eval_settings_metadata,
    list_fetch_settings_metadata,
    list_flake_settings_metadata,
    list_settings_metadata,
)
from nanopynix.store import StoreHandle
from nanopynix.types import NixArg, NixDeepValue, NixValue
from nanopynix.verbosity import LogLevelInput, normalize_log_level
from nanopynix_expr import EvalState, Value, eval_file, init_libexpr, register_primop
from nanopynix_fetchers import input_from_attrs, input_from_url
from nanopynix_flake import get_flake, lock_flake, parse_flake_ref
from nanopynix_main import init_nix, init_plugins
from nanopynix_store import BuildMode, open_store
from nanopynix_util import (
    build_info,
    current_system,
    enable_experimental_feature,
    get_setting,
    get_verbosity,
    init_libstore,
    install_logger,
    list_settings,
    remove_logger,
    set_setting,
    set_verbosity,
)

# Backward-compatible alias for StoreHandle
Store = StoreHandle

__all__ = [
    "BuildMode",
    "build_info",
    "LockedFlakeHandle",
    "LogLevel",
    "LogLevelInput",
    "NixAssertionError",
    "NixCoercionError",
    "NixError",
    "NixEvalSettings",
    "NixFetchSettings",
    "NixFlakeSettings",
    "NixSettingMetadata",
    "NixSettings",
    "NixSettingsEnv",
    "NixType",
    "NixTypeError",
    "NixValue",
    "ParseError",
    "PathInfo",
    "PrimOpSpec",
    "RestrictedPathError",
    "ResultType",
    "Session",
    "SettingsDrift",
    "Store",
    "StoreError",
    "StoreHandle",
    "StorePath",
    "ThrownError",
    "UndefinedVarError",
    "UnresolvedValueError",
    "UsageError",
    "Value",
    "ValueAttrs",
    "ValueHandle",
    "ValueList",
    "ValueProxy",
    "ValueReleasedError",
    "WorkerBusyError",
    "WorkerDiedError",
    "WrongNixTypeError",
    "check_all_settings_model_drift",
    "check_settings_model_drift",
    "current_system",
    "enable_experimental_feature",
    "eval_file",
    "from_yaml",
    "from_yaml11",
    "from_yaml11_stream",
    "from_yaml_stream",
    "get_flake",
    "get_setting",
    "get_verbosity",
    "init_libexpr",
    "init_libstore",
    "init_nix",
    "init_plugins",
    "input_from_attrs",
    "input_from_url",
    "install_logger",
    "list_eval_settings_metadata",
    "list_fetch_settings_metadata",
    "list_flake_settings_metadata",
    "list_settings",
    "list_settings_metadata",
    "lock_flake",
    "normalize_log_level",
    "open_store",
    "parse_flake_ref",
    "register_primop",
    "remove_logger",
    "set_setting",
    "set_verbosity",
    "strip_ansi",
    "to_yaml",
    "yaml_primops",
]
