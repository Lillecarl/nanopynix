# ruff: noqa: F401
# pyright: reportUnusedImport=false
"""nanopynix — nanobind-based Python bindings for Nix."""

from __future__ import annotations

from nanopynix_bindings.expr import EvalState, PrimopError, Value, eval_file, init_libexpr, register_primop
from nanopynix_bindings.fetchers import input_from_attrs, input_from_url
from nanopynix_bindings.flake import get_flake, lock_flake, parse_flake_ref
from nanopynix_bindings.main import init_nix as _init_nix_raw, init_plugins
from nanopynix_bindings.store import (
    BuildMode as BuildMode,
    open_store as open_store,
    process_connection as process_connection,
    register_store_implementation as register_store_implementation,
)
from nanopynix_bindings.util import (
    build_info,  # type: ignore[reportUnknownVariableType] -- C++ extension without type stubs
    current_system,
    enable_experimental_feature,
    get_setting,
    get_verbosity,
    init_libstore as _init_libstore_raw,
    install_logger,
    list_settings,
    remove_logger,
    set_setting,
    set_verbosity,
)
from nanopynix_proto.nix.common import LogLevel
from nanopynix_proto.nix.store import GcAction as GcAction
from strip_ansi import strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

from nanopynix import inproc as inproc, rpc as rpc
from nanopynix._process_title import set_manager_title as set_manager_title
from nanopynix.exceptions import (
    BadStorePathError,
    BuildError,
    BuildHashMismatchError,
    BuildTimedOutError,
    CachedBuildFailureError,
    DependencyFailedError,
    EvalError,
    EvalHashMismatchError,
    EvalSessionClosedError,
    ForeignValueError,
    HashMismatchError,
    InfiniteRecursionError,
    InputRejectedError,
    InvalidPathError,
    ListIndexError,
    LockedFlakeReleasedError,
    LogLimitExceededError,
    MiscBuildError,
    MissingArgumentError,
    MissingAttributeError,
    NixAssertionError,
    NixError,
    NixSysError,
    NixTypeError,
    NoSubstitutersError,
    NotDeterministicError,
    ObjectLifetimeError,
    ObjectMisuseError,
    OutputRejectedError,
    ParseError,
    PermanentBuildError,
    RestrictedPathError,
    SessionClosedError,
    StoreClosedError,
    StoreError,
    ThrownError,
    TransientBuildError,
    UndefinedVarError,
    UnimplementedError,
    UnresolvedValueError,
    UnsupportedError,
    UsageError,
    ValueReleasedError,
    WrongNixTypeError,
)
from nanopynix.logging import LogCapture, LogCollector
from nanopynix.models import (
    BuildResult,
    Derivation,
    DerivationOutput,
    DerivationOutputs,
    FlakeRef,
    GcResult,
    GcRoot,
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
    DEFAULT_EXPERIMENTAL_FEATURES,
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
from nanopynix.store_impl import DISPATCHABLE_METHODS as DISPATCHABLE_METHODS, StoreImpl as StoreImpl
from nanopynix.verbosity import LogLevelInput, normalize_log_level


def init_nix(load_config: bool = True) -> None:
    """Initialize Nix, then enable nanopynix's default experimental features.

    Enabling the features here, rather than leaving it to whoever opens a
    store, is load-bearing: Nix latches some of them at store *construction*
    but re-checks them at *query* time. ``LocalStore`` prepares its realisation
    SQL statements only when ``ca-derivations`` is on at construction
    (``local-store.cc:356``), while ``queryRealisationUncached`` re-tests the
    flag and dereferences those statements (``:1563``). A store built before
    the feature was enabled, then queried after it was turned on, therefore
    trips ``assert(stmt.stmt)`` and aborts the process -- SIGABRT, not an
    exception, so there is nothing a caller could have caught.

    Since ``initNix`` has to run before any libstore call anyway, doing it here
    means every store nanopynix can open is constructed with the defaults
    already in force. ``Session`` applies the same settings again through
    ``runtime.initialize``; that is additive and harmless.
    """
    _init_nix_raw(load_config=load_config)
    _enable_default_experimental_features()


def init_libstore(load_config: bool = True) -> None:
    """Initialize libstore only, then enable nanopynix's default experimental features.

    The lighter of the two entry points, and it needs the same treatment as
    ``init_nix`` for the same reason -- see that function for why the ordering
    matters. Wrapping only one of them would leave the hazard reachable through
    the other.
    """
    _init_libstore_raw(load_config=load_config)
    _enable_default_experimental_features()


def _enable_default_experimental_features() -> None:
    for feature in DEFAULT_EXPERIMENTAL_FEATURES:
        enable_experimental_feature(feature)


__all__ = [
    "DISPATCHABLE_METHODS",
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
    "BadStorePathError",
    "BuildError",
    "BuildHashMismatchError",
    "BuildMode",
    "BuildTimedOutError",
    "CachedBuildFailureError",
    "DependencyFailedError",
    "EvalHashMismatchError",
    "GcAction",
    "GcResult",
    "GcRoot",
    "HashMismatchError",
    "InputRejectedError",
    "InvalidPathError",
    "ListIndexError",
    "LockedFlakeReleasedError",
    "LogLevel",
    "LogLevelInput",
    "LogLimitExceededError",
    "MiscBuildError",
    "MissingAttributeError",
    "NanopynixSettings",
    "NixAssertionError",
    "NixError",
    "NixEvalSettings",
    "NixFetchSettings",
    "NixFlakeSettings",
    "NixSettingMetadata",
    "NixSettings",
    "NixSettingsEnv",
    "NixSysError",
    "NixType",
    "NixTypeError",
    "NoSubstitutersError",
    "NotDeterministicError",
    "ObjectLifetimeError",
    "ObjectMisuseError",
    "OutputRejectedError",
    "ParseError",
    "PathInfo",
    "PermanentBuildError",
    "PrimOpSpec",
    "PrimopError",
    "RestrictedPathError",
    "ResultType",
    "SessionClosedError",
    "SettingsDrift",
    "StoreClosedError",
    "StoreError",
    "StoreImpl",
    "StorePath",
    "ThrownError",
    "TransientBuildError",
    "UndefinedVarError",
    "UnimplementedError",
    "UnresolvedValueError",
    "UnsupportedError",
    "UsageError",
    "Value",
    "ValueHandle",
    "ValueReleasedError",
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
    "process_connection",
    "register_primop",
    "register_store_implementation",
    "remove_logger",
    "rpc",
    "set_manager_title",
    "set_setting",
    "set_verbosity",
    "strip_ansi",
    "to_yaml",
    "yaml_primops",
]
