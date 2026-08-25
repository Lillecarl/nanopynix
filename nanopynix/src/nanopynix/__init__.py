# pyright: reportUnusedImport=false
# Justifies the pragma above. The block below the table is the type checker's
# copy of this module's public surface: every import in it is a deliberate
# re-export that no runtime line reads, so 'unused' is exactly what a correct
# entry looks like. The reason sits here rather than after the code because
# pyright rejects trailing text on its pragma -- it reports a directive error
# and silently stops suppressing.
"""nanopynix — nanobind-based Python bindings for Nix."""

from __future__ import annotations

import importlib
import typing

# `import typing`, rather than `from typing import ...`. Two rules of this
# repository meet here:
#
# - `tests/meta/test_public_surface.py` fails on a public name that the package
#   binds and `__all__` does not carry. It exempts a *module*, because
#   `from nanopynix.x import y` binds one as a side effect, so `typing` passes
#   where `TYPE_CHECKING` did not.
# - beartype wraps `__getattr__` and resolves its return annotation when the
#   function runs. A `TYPE_CHECKING`-only name in that annotation therefore
#   makes every `from nanopynix import rpc` raise
#   `BeartypeCallHintForwardRefException`, which is a collection error and not
#   a type error.
#
# Neither import costs anything: both are in `sys.modules` before this module
# loads.

# **Every public name of this package resolves on first use, and none of them
# resolves at import.**
#
# This block is for the type checker, which cannot read the table below.
# pyright reports each entry as unused, and an unused import is what a correct
# entry looks like here, so the file turns that rule off at the top.
if typing.TYPE_CHECKING:
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
        install_logger,
        list_settings,
        remove_logger,
        set_verbosity,
    )
    from nanopynix_proto.nix.common import (
        GcAction as GcAction,
        LogLevel as LogLevel,
    )

    from nanopynix import (
        inproc as inproc,
        rpc as rpc,
        stores as stores,
    )
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
    from nanopynix.libstore import init_libstore as init_libstore
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
        attrs_to_python,
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


#: The module that defines each public name, for :func:`__getattr__`.
#:
#: **The package used to import all of this at load time, and a program pays
#: for what it reads.** Issue #123 measured it: ``import nanopynix`` was
#: 598 ms against 16 ms for a bare interpreter, and the four heaviest modules
#: under it -- the generated protocol, the settings models, the store registry
#: and the namespace helper -- are read by no consumer at once. The issue names
#: a real case: a planner of ``ddrn/examples/venv-graph`` spent 97% of its run
#: on this import, and the one name it read was ``init_libstore``.
#:
#: A module ``__getattr__`` (PEP 562) is the shape this repository permits.
#: It is a package-level construct and not an import inside a function, so the
#: ban in ``CLAUDE.md`` does not reach it, and ``from nanopynix import X``
#: keeps working: ``IMPORT_FROM`` falls back to ``getattr`` on the module.
#:
#: ``tests/meta/test_public_surface.py`` checks this table against ``__all__``
#: in both directions, so a name cannot be dropped from one and kept in the
#: other.
_NAME_TO_MODULE: typing.Final[dict[str, str]] = {
    "AsyncEvalSession": "nanopynix.protocols",
    "AsyncLockedFlake": "nanopynix.protocols",
    "AsyncReplSession": "nanopynix.protocols",
    "AsyncSession": "nanopynix.protocols",
    "AsyncStore": "nanopynix.protocols",
    "AsyncValue": "nanopynix.protocols",
    "AsyncVerbosityController": "nanopynix.protocols",
    "BadStorePathError": "nanopynix.exceptions",
    "BuildError": "nanopynix.exceptions",
    "BuildHashMismatchError": "nanopynix.exceptions",
    "BuildMode": "nanopynix_bindings.store",
    "BuildResult": "nanopynix.models",
    "BuildTimedOutError": "nanopynix.exceptions",
    "CachedBuildFailureError": "nanopynix.exceptions",
    "DEFAULT_EXPERIMENTAL_FEATURES": "nanopynix.settings",
    "DISPATCHABLE_METHODS": "nanopynix.store_impl",
    "DependencyFailedError": "nanopynix.exceptions",
    "Derivation": "nanopynix.models",
    "DerivationOutput": "nanopynix.models",
    "DerivationOutputs": "nanopynix.models",
    "DerivedPath": "nanopynix.models",
    "EngineError": "nanopynix.exceptions",
    "EvalError": "nanopynix.exceptions",
    "EvalHashMismatchError": "nanopynix.exceptions",
    "EvalSessionClosedError": "nanopynix.exceptions",
    "EvalState": "nanopynix_bindings.expr",
    "EvaluatorAbandonedError": "nanopynix.exceptions",
    "FlakeRef": "nanopynix.models",
    "ForeignValueError": "nanopynix.exceptions",
    "ForkedSessionError": "nanopynix.exceptions",
    "GcAction": "nanopynix_proto.nix.common",
    "GcResult": "nanopynix.models",
    "GcRoot": "nanopynix.models",
    "HashMismatchError": "nanopynix.exceptions",
    "InfiniteRecursionError": "nanopynix.exceptions",
    "Input": "nanopynix.models",
    "InputRejectedError": "nanopynix.exceptions",
    "InvalidPathError": "nanopynix.exceptions",
    "ListIndexError": "nanopynix.exceptions",
    "LockedFlake": "nanopynix.models",
    "LockedFlakeReleasedError": "nanopynix.exceptions",
    "LockedNode": "nanopynix.models",
    "LogCapture": "nanopynix.logging",
    "LogCollector": "nanopynix.logging",
    "LogEvent": "nanopynix.models",
    "LogLevel": "nanopynix_proto.nix.common",
    "LogLevelInput": "nanopynix.verbosity",
    "LogLimitExceededError": "nanopynix.exceptions",
    "MiscBuildError": "nanopynix.exceptions",
    "MissingArgumentError": "nanopynix.exceptions",
    "MissingAttributeError": "nanopynix.exceptions",
    "MissingInfo": "nanopynix.models",
    "NamespaceSupport": "nanopynix.namespace",
    "NanopynixSettings": "nanopynix.settings",
    "NixAssertionError": "nanopynix.exceptions",
    "NixError": "nanopynix.exceptions",
    "NixEvalSettings": "nanopynix.settings",
    "NixEvaluatorSettings": "nanopynix.settings",
    "NixFetchSettings": "nanopynix.settings",
    "NixFlakeSettings": "nanopynix.settings",
    "NixGlobalSettings": "nanopynix.settings",
    "NixSettingMetadata": "nanopynix.settings",
    "NixSettings": "nanopynix.settings",
    "NixSettingsEnv": "nanopynix.settings",
    "NixStoreDefaults": "nanopynix.settings",
    "NixSysError": "nanopynix.exceptions",
    "NixType": "nanopynix.models",
    "NixTypeError": "nanopynix.exceptions",
    "NoSubstitutersError": "nanopynix.exceptions",
    "NotDeterministicError": "nanopynix.exceptions",
    "ObjectLifetimeError": "nanopynix.exceptions",
    "ObjectMisuseError": "nanopynix.exceptions",
    "OutputRejectedError": "nanopynix.exceptions",
    "OverlayNamespace": "nanopynix.namespace",
    "ParseError": "nanopynix.exceptions",
    "PathInfo": "nanopynix.models",
    "PermanentBuildError": "nanopynix.exceptions",
    "PrefixedEnvSettingsSource": "nanopynix.settings",
    "PrimOpSpec": "nanopynix.models",
    "PrimopError": "nanopynix_bindings.expr",
    "RestrictedPathError": "nanopynix.exceptions",
    "ResultType": "nanopynix.models",
    "STORE_EXEC_TOOL": "nanopynix.store_exec",
    "SessionClosedError": "nanopynix.exceptions",
    "SettingNotLiveError": "nanopynix.exceptions",
    "SettingOutOfScopeError": "nanopynix.exceptions",
    "SettingsDrift": "nanopynix.settings",
    "SettingsProvenance": "nanopynix.settings",
    "StoreClosedError": "nanopynix.exceptions",
    "StoreError": "nanopynix.exceptions",
    "StoreImpl": "nanopynix.store_impl",
    "StorePath": "nanopynix.models",
    "ThrownError": "nanopynix.exceptions",
    "TransientBuildError": "nanopynix.exceptions",
    "UndefinedVarError": "nanopynix.exceptions",
    "UnimplementedError": "nanopynix.exceptions",
    "UnresolvedValueError": "nanopynix.exceptions",
    "UnsupportedError": "nanopynix.exceptions",
    "UsageError": "nanopynix.exceptions",
    "Value": "nanopynix_bindings.expr",
    "ValueReleasedError": "nanopynix.exceptions",
    "WorkerDiedError": "nanopynix.exceptions",
    "WorkerSignaledError": "nanopynix.exceptions",
    "WrongNixTypeError": "nanopynix.exceptions",
    "attrs_to_python": "nanopynix.models",
    "build_info": "nanopynix_bindings.util",
    "check_all_settings_model_drift": "nanopynix.settings",
    "check_settings_model_drift": "nanopynix.settings",
    "current_system": "nanopynix_bindings.util",
    "enable_experimental_feature": "nanopynix_bindings.util",
    "enter_overlay_namespace": "nanopynix.namespace",
    "eval_counters_enabled": "nanopynix_bindings.expr",
    "eval_file": "nanopynix_bindings.expr",
    "from_yaml": "nanopynix.primops",
    "from_yaml11": "nanopynix.primops",
    "from_yaml11_stream": "nanopynix.primops",
    "from_yaml_stream": "nanopynix.primops",
    "get_flake": "nanopynix_bindings.flake",
    "get_verbosity": "nanopynix_bindings.util",
    "init_libexpr": "nanopynix_bindings.expr",
    "init_libstore": "nanopynix.libstore",
    "input_from_attrs": "nanopynix_bindings.fetchers",
    "input_from_url": "nanopynix_bindings.fetchers",
    "install_logger": "nanopynix_bindings.util",
    "is_pseudo_url": "nanopynix_bindings.expr",
    "list_eval_settings_metadata": "nanopynix.settings",
    "list_fetch_settings_metadata": "nanopynix.settings",
    "list_flake_settings_metadata": "nanopynix.settings",
    "list_settings": "nanopynix_bindings.util",
    "list_settings_metadata": "nanopynix.settings",
    "lock_flake": "nanopynix_bindings.flake",
    "normalize_log_level": "nanopynix.verbosity",
    "open_store": "nanopynix_bindings.store",
    "parse_flake_ref": "nanopynix_bindings.flake",
    "probe_namespace_support": "nanopynix.namespace",
    "process_connection": "nanopynix_bindings.store",
    "register_primop": "nanopynix_bindings.expr",
    "register_store_implementation": "nanopynix_bindings.store",
    "remove_logger": "nanopynix_bindings.util",
    "set_eval_counters_enabled": "nanopynix_bindings.expr",
    "set_manager_title": "nanopynix._process_title",
    "set_verbosity": "nanopynix_bindings.util",
    "store_exec_prefix": "nanopynix.store_exec",
    "strip_ansi": "nanopynix._ansi",
    "to_yaml": "nanopynix.primops",
    "yaml_primops": "nanopynix.primops",
}

#: The submodules that :func:`__getattr__` imports on attribute access.
#:
#: ``from nanopynix.exceptions import NixError`` binds ``exceptions`` on the
#: package as a side effect, so code that reads ``nanopynix.exceptions`` used
#: to work by luck. Nothing imports the package eagerly any more, so the luck
#: is gone and the table replaces it.
_LAZY_SUBMODULES: typing.Final[frozenset[str]] = frozenset(
    {
        "_ansi",
        "_core",
        "_env",
        "_features",
        "_fork",
        "_process_title",
        "_typechecking",
        "_wire",
        "exceptions",
        "inproc",
        "libstore",
        "logging",
        "models",
        "namespace",
        "primops",
        "protocols",
        "rpc",
        "settings",
        "store_exec",
        "store_impl",
        "stores",
        "verbosity",
    }
)


def __getattr__(name: str) -> typing.Any:
    """Resolve a public name, and cache it in the module namespace.

    Writing the result into ``globals()`` means this runs one time for each
    name. Python calls a module ``__getattr__`` only when the ordinary
    attribute lookup fails, so the second read takes the global directly.
    """
    origin = _NAME_TO_MODULE.get(name)
    if origin is not None:
        value = getattr(importlib.import_module(origin), name)
    elif name in _LAZY_SUBMODULES:
        value = importlib.import_module(f"nanopynix.{name}")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Report the public surface, which ``vars()`` no longer holds.

    ``dir()`` on a module reads ``__dict__`` when a module defines no
    ``__dir__``, and this one starts nearly empty. Tab completion in a REPL
    and ``inspect.getmembers`` both go through here.
    """
    return sorted(set(__all__) | set(_LAZY_SUBMODULES) | set(globals()))


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
    "attrs_to_python",
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
