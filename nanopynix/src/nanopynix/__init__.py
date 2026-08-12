# pyright: reportUnusedImport=false
# Justifies both pragmas above. This module is nanopynix's public surface:
# every import in it is a deliberate re-export, so 'unused' is exactly
# what a correct entry in it looks like. The reason sits here rather than
# after the codes because pyright rejects trailing text on its pragma --
# it reports a directive error and silently stops suppressing.
"""nanopynix — nanobind-based Python bindings for Nix."""

from __future__ import annotations

from nanopynix_bindings.expr import (
    EvalState,
    PrimopError,
    Value,
    eval_counters_enabled,
    eval_file,
    init_libexpr,
    is_pseudo_url,
    register_primop,
    set_eval_counters_enabled,
)
from nanopynix_bindings.fetchers import input_from_attrs, input_from_url
from nanopynix_bindings.flake import get_flake, lock_flake, parse_flake_ref
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
    get_verbosity,
    init_libstore as _init_libstore_raw,
    install_logger,
    list_settings,
    remove_logger,
    set_verbosity,
)
from nanopynix_proto.nix.common import LogLevel
from nanopynix_proto.nix.store import GcAction as GcAction

from nanopynix import inproc as inproc, rpc as rpc, stores as stores
from nanopynix._ansi import strip_ansi as strip_ansi
from nanopynix._process_title import set_manager_title as set_manager_title
from nanopynix.exceptions import (
    BadStorePathError,
    BuildError,
    BuildHashMismatchError,
    BuildTimedOutError,
    CachedBuildFailureError,
    DependencyFailedError,
    EngineError,
    EvalError,
    EvalHashMismatchError,
    EvalSessionClosedError,
    EvaluatorAbandonedError,
    ForeignValueError,
    ForkedSessionError,
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
    SettingNotLiveError,
    SettingOutOfScopeError,
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
    WorkerDiedError,
    WorkerSignaledError,
    WrongNixTypeError,
)
from nanopynix.logging import LogCapture, LogCollector
from nanopynix.models import (
    BuildResult,
    Derivation,
    DerivationOutput,
    DerivationOutputs,
    DerivedPath,
    FlakeRef,
    GcResult,
    GcRoot,
    Input,
    LockedFlake,
    LockedNode,
    LogEvent,
    MissingInfo,
    NixType,
    PathInfo,
    PrimOpSpec,
    ResultType,
    StorePath,
)
from nanopynix.namespace import (
    NamespaceSupport as NamespaceSupport,
    OverlayNamespace as OverlayNamespace,
    enter_overlay_namespace as enter_overlay_namespace,
    probe_namespace_support as probe_namespace_support,
)
from nanopynix.primops import from_yaml, from_yaml11, from_yaml11_stream, from_yaml_stream, to_yaml, yaml_primops
from nanopynix.protocols import (
    AsyncEvalSession,
    AsyncLockedFlake,
    AsyncReplSession,
    AsyncSession,
    AsyncStore,
    AsyncValue,
    AsyncVerbosityController,
)
from nanopynix.settings import (
    DEFAULT_EXPERIMENTAL_FEATURES,
    NanopynixSettings,
    NixEvalSettings,
    NixEvaluatorSettings,
    NixFetchSettings,
    NixFlakeSettings,
    NixGlobalSettings,
    NixSettingMetadata,
    NixSettings,
    NixSettingsEnv,
    NixStoreDefaults,
    PrefixedEnvSettingsSource,
    SettingsDrift,
    SettingsProvenance,
    check_all_settings_model_drift,
    check_settings_model_drift,
    list_eval_settings_metadata,
    list_fetch_settings_metadata,
    list_flake_settings_metadata,
    list_settings_metadata,
)
from nanopynix.store_exec import STORE_EXEC_TOOL as STORE_EXEC_TOOL, store_exec_prefix as store_exec_prefix
from nanopynix.store_impl import DISPATCHABLE_METHODS as DISPATCHABLE_METHODS, StoreImpl as StoreImpl
from nanopynix.verbosity import LogLevelInput, normalize_log_level


def init_libstore(load_config: bool = True) -> None:
    """Initialize libstore, then enable nanopynix's default experimental features.

    The one Nix initialisation entry point nanopynix offers. There used to be a
    second, ``init_nix``, wrapping ``nix::initNix``; it is gone because
    everything ``initNix`` adds over ``initLibStore`` is a process-wide side
    effect a library has no business imposing on its host -- a signal-handler
    thread, ``SIGCHLD`` reset to ``SIG_DFL``, a ``SIGSEGV`` handler, an
    ``NIX_SIG_MULTI_INT`` handler, ``umask(0022)``, a ``RLIMIT_NOFILE`` bump
    and a static buffer installed on ``std::cerr``. Python has its own signal
    machinery, and nothing in nanopynix ever called it.

    Enabling the features here, rather than leaving it to whoever opens a
    store, is load-bearing: Nix latches some of them at store *construction*
    but re-checks them at *query* time. ``LocalStore`` prepares its realisation
    SQL statements only when ``ca-derivations`` is on at construction
    (``local-store.cc:356``), while ``queryRealisationUncached`` re-tests the
    flag and dereferences those statements (``:1563``). A store built before
    the feature was enabled, then queried after it was turned on, therefore
    trips ``assert(stmt.stmt)`` and aborts the process -- SIGABRT, not an
    exception, so there is nothing a caller could have caught.

    Since libstore has to be initialised before any libstore call anyway, doing
    it here means every store nanopynix can open is constructed with the
    defaults already in force. ``Session`` enables the same features again
    through ``runtime.initialize``, which calls
    :func:`enable_experimental_feature` at the same point of its own sequence;
    that is additive and harmless.
    """
    _init_libstore_raw(load_config=load_config)
    _enable_default_experimental_features()


def _enable_default_experimental_features() -> None:
    for feature in DEFAULT_EXPERIMENTAL_FEATURES:
        enable_experimental_feature(feature)


__all__ = [
    "DEFAULT_EXPERIMENTAL_FEATURES",
    "DISPATCHABLE_METHODS",
    "STORE_EXEC_TOOL",
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
    "BadStorePathError",
    "BuildError",
    "BuildHashMismatchError",
    "BuildMode",
    "BuildResult",
    "BuildTimedOutError",
    "CachedBuildFailureError",
    "DependencyFailedError",
    "Derivation",
    "DerivationOutput",
    "DerivationOutputs",
    "DerivedPath",
    "EngineError",
    "EvalError",
    "EvalHashMismatchError",
    "EvalSessionClosedError",
    "EvalState",
    "EvaluatorAbandonedError",
    "FlakeRef",
    "ForeignValueError",
    "ForkedSessionError",
    "GcAction",
    "GcResult",
    "GcRoot",
    "HashMismatchError",
    "InfiniteRecursionError",
    "Input",
    "InputRejectedError",
    "InvalidPathError",
    "ListIndexError",
    "LockedFlake",
    "LockedFlakeReleasedError",
    "LockedNode",
    "LogCapture",
    "LogCollector",
    "LogEvent",
    "LogLevel",
    "LogLevelInput",
    "LogLimitExceededError",
    "MiscBuildError",
    "MissingArgumentError",
    "MissingAttributeError",
    "MissingInfo",
    "NamespaceSupport",
    "NanopynixSettings",
    "NixAssertionError",
    "NixError",
    "NixEvalSettings",
    "NixEvaluatorSettings",
    "NixFetchSettings",
    "NixFlakeSettings",
    "NixGlobalSettings",
    "NixSettingMetadata",
    "NixSettings",
    "NixSettingsEnv",
    "NixStoreDefaults",
    "NixSysError",
    "NixType",
    "NixTypeError",
    "NoSubstitutersError",
    "NotDeterministicError",
    "ObjectLifetimeError",
    "ObjectMisuseError",
    "OutputRejectedError",
    "OverlayNamespace",
    "ParseError",
    "PathInfo",
    "PermanentBuildError",
    "PrefixedEnvSettingsSource",
    "PrimOpSpec",
    "PrimopError",
    "RestrictedPathError",
    "ResultType",
    "SessionClosedError",
    "SettingNotLiveError",
    "SettingOutOfScopeError",
    "SettingsDrift",
    "SettingsProvenance",
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
    "ValueReleasedError",
    "WorkerDiedError",
    "WorkerSignaledError",
    "WrongNixTypeError",
    "build_info",
    "check_all_settings_model_drift",
    "check_settings_model_drift",
    "current_system",
    "enable_experimental_feature",
    "enter_overlay_namespace",
    "eval_counters_enabled",
    "eval_file",
    "from_yaml",
    "from_yaml11",
    "from_yaml11_stream",
    "from_yaml_stream",
    "get_flake",
    "get_verbosity",
    "init_libexpr",
    "init_libstore",
    "inproc",
    "input_from_attrs",
    "input_from_url",
    "install_logger",
    "is_pseudo_url",
    "list_eval_settings_metadata",
    "list_fetch_settings_metadata",
    "list_flake_settings_metadata",
    "list_settings",
    "list_settings_metadata",
    "lock_flake",
    "normalize_log_level",
    "open_store",
    "parse_flake_ref",
    "probe_namespace_support",
    "process_connection",
    "register_primop",
    "register_store_implementation",
    "remove_logger",
    "rpc",
    "set_eval_counters_enabled",
    "set_manager_title",
    "set_verbosity",
    "store_exec_prefix",
    "stores",
    "strip_ansi",
    "to_yaml",
    "yaml_primops",
]
