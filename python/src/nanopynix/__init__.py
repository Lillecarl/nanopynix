# ruff: noqa: F401
# pyright: reportUnusedImport=false
"""nanopynix — nanobind-based Python bindings for Nix."""

from __future__ import annotations

from nanopynix_proto.nix.common import LogLevel
from nanopynix_proto.nix.store import GcAction as GcAction
from strip_ansi import strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

from nanopynix import inproc as inproc
from nanopynix._pool import WorkerDiedError
from nanopynix._process_title import set_manager_title as set_manager_title
from nanopynix._session import EvalSession, LockedFlakeHandle, ReplSession, ValueAttrs, ValueList, ValueProxy
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
    GcResult,
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
from nanopynix.protocols import (
    AsyncEvalSession,
    AsyncLockedFlake,
    AsyncReplSession,
    AsyncStore,
    AsyncValue,
    AsyncVerbosityController,
)
from nanopynix.settings import (
    NanopynixSettings,
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
from nanopynix.store import Store as Store
from nanopynix.store import StoreHandle as StoreHandle
from nanopynix.types import NixArg, NixDeepValue, NixValue
from nanopynix.verbosity import LogLevelInput, normalize_log_level
from nanopynix_expr import EvalState, Value, eval_file, init_libexpr, register_primop
from nanopynix_fetchers import input_from_attrs, input_from_url
from nanopynix_flake import get_flake, lock_flake, parse_flake_ref
from nanopynix_main import init_nix, init_plugins
from nanopynix_store import BuildMode, open_store
from nanopynix_util import (
    build_info,  # type: ignore[reportUnknownVariableType] -- C++ extension without type stubs
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

__all__ = [
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
    "BuildMode",
    "EvalSession",
    "GcAction",
    "GcResult",
    "LockedFlakeHandle",
    "LogLevel",
    "LogLevelInput",
    "NanopynixSettings",
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
    "ReplSession",
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
    "WorkerDiedError",
    "WrongNixTypeError",
    "build_info",
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
    "inproc",
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
    "set_manager_title",
    "set_setting",
    "set_verbosity",
    "strip_ansi",
    "to_yaml",
    "yaml_primops",
]
