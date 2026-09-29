"""The Nix engine, and the only module of nanopynix that imports it.

Every other module takes the engine from here. The engine is huggorm's
generated bindings, and ``huggorm_bindings-stubs`` types each name.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any, Final, NoReturn

import huggorm_bindings
from huggorm_bindings import (
    AttrDoc as AttrDoc,
    BuildMode as BuildMode,
    ContentAddressMethod as ContentAddressMethod,
    DerivationOutputCAFixed as DerivationOutputCAFixed,
    DerivationOutputCAFloating as DerivationOutputCAFloating,
    DerivationOutputDeferred as DerivationOutputDeferred,
    DerivationOutputImpure as DerivationOutputImpure,
    DerivationOutputInputAddressed as DerivationOutputInputAddressed,
    DerivedPathBuilt as DerivedPathBuilt,
    Doc as Doc,
    ErrorInfo as ErrorInfo,
    EvalState as EvalState,
    FlakeRef as FlakeRef,
    GCAction as GCAction,
    GCOptions as GCOptions,
    HashAlgorithm as HashAlgorithm,
    KeyedBuildResult as KeyedBuildResult,
    LockedFlake as LockedFlake,
    LogRecord as LogRecord,
    OutputsSpec as OutputsSpec,
    Position as Position,
    RegistryWrite as RegistryWrite,
    Repl as Repl,
    Store as Store,
    StorePath as StorePath,
    StoreReferenceAuto as StoreReferenceAuto,
    StoreReferenceDaemon as StoreReferenceDaemon,
    StoreReferenceLocal as StoreReferenceLocal,
    StoreReferenceSpecified as StoreReferenceSpecified,
    Value as Value,
    begin_interrupt_scope as begin_interrupt_scope,
    begin_request as begin_request,
    boehm_gc as boehm_gc,
    cancel_interrupt_scope as cancel_interrupt_scope,
    collect_garbage as collect_garbage,
    collector_owner_thread as collector_owner_thread,
    current_request as current_request,
    current_system as current_system,
    default_verbosity as default_verbosity,
    enable_experimental_feature as enable_experimental_feature,
    end_interrupt_scope as end_interrupt_scope,
    errors as errors,  # pyright: ignore[reportMissingTypeStubs] -- errors.py ships as source, with no errors.pyi
    eval_counters_enabled as eval_counters_enabled,
    eval_settings_json as eval_settings_json,
    fetch_settings_json as fetch_settings_json,
    filter_ansi_escapes as filter_ansi_escapes,
    flake_settings_json as flake_settings_json,
    forget_interrupt_scope as forget_interrupt_scope,
    gc_release_thread as gc_release_thread,
    gc_stats as gc_stats,
    get_setting as get_setting,
    is_experimental_feature as is_experimental_feature,
    is_pseudo_url as is_pseudo_url,
    list_settings as list_settings,
    load_config as load_config,
    log_message as log_message,
    nix_version as nix_version,
    parse_flake_ref as parse_flake_ref,
    parse_nix_path as parse_nix_path,
    parse_store_reference as parse_store_reference,
    process_verbosity as process_verbosity,
    registry_add as registry_add,
    registry_entries as registry_entries,
    registry_pin as registry_pin,
    registry_remove as registry_remove,
    render_store_reference as render_store_reference,
    reset_overridden as reset_overridden,
    set_default_verbosity as set_default_verbosity,
    set_eval_counters_enabled as set_eval_counters_enabled,
    set_setting as set_setting,
    set_thread_verbosity as set_thread_verbosity,
    settings_json as settings_json,
    start_collector as start_collector,
    store_types_json as store_types_json,
    subscribe_process_logs as subscribe_process_logs,
    thread_verbosity as thread_verbosity,
    unsubscribe_process_logs as unsubscribe_process_logs,
    user_registry_path as user_registry_path,
)
from huggorm_bindings.errors import (  # pyright: ignore[reportMissingTypeStubs] -- errors.py ships as source, and huggorm_bindings-stubs has no errors.pyi
    BadStorePath as BadStorePath,
    EvalBaseError as EvalBaseError,
    EvalError as EvalError,
    Interrupted as Interrupted,
    InvalidPath as InvalidPath,
    NixAssertionError as NixAssertionError,
    NixError as NixError,
    NixTypeError as NixTypeError,
    ParseError as ParseError,
    ThrownError as ThrownError,
)

#: Whether ``GCOptions`` takes its paths as a union, as Nix 2.35 does. A shape
#: and not a version, so the rolling git build answers for itself.
GC_PATHS_UNION: Final = hasattr(huggorm_bindings, "GCSpecificPaths")

#: The package whose exceptions are Nix's own.
ENGINE = "huggorm_bindings"


def is_engine_error(exc: BaseException) -> bool:
    """Tell whether the engine raised *exc* for a Nix C++ exception.

    The class name of such an exception is the C++ name, so
    :func:`~nanopynix.exceptions.exception_for_nix_type` can look it up. The
    module decides, not the name: some Nix classes share a name with a Python
    builtin, such as ``TypeError``.
    """
    return type(exc).__module__.startswith(ENGINE)


def gc_options(action: GCAction, ignore_liveness: bool, paths: list[StorePath], max_freed: int | None) -> GCOptions:
    """A collection's options, for either shape of ``GCOptions``.

    Before 2.35, an empty list means no path. From 2.35, an absent union
    means the whole store, so an empty list stays a list of no path when the
    action deletes specific paths.
    """
    if GC_PATHS_UNION:
        where = huggorm_bindings.GCSpecificPaths(paths) if paths or action == GCAction.DELETE_SPECIFIC else None
        return GCOptions(action, ignore_liveness, where, max_freed)
    return GCOptions(action, ignore_liveness, paths, max_freed)


def get_env_sh_path() -> Path:
    """Nix's ``get-env.sh``, which ``huggorm_bindings`` carries."""
    return Path(str(importlib.resources.files(huggorm_bindings) / "get-env.sh"))


#: The level of Nix's logging on the calling thread.
get_verbosity = thread_verbosity
#: Set the level of Nix's logging on the calling thread.
set_verbosity = set_thread_verbosity


def build_info() -> dict[str, Any]:
    """The linked Nix's version, and what this build of nanopynix can do.

    ``boehm_gc`` is a fact about the linked libexpr. The other capabilities
    are nanopynix features; each one huggorm cannot serve yet is ``False``,
    and present, because callers index them.
    """
    return {
        "nix_version": nix_version(),
        "capabilities": {
            "boehm_gc": boehm_gc(),
            "dynamic_primop_registration": True,
            "eval_statistics": True,
            "store_impl_read_derivation": False,
        },
    }


# --- Not in huggorm yet -------------------------------------------------
#
# Each name below is a nanopynix feature that huggorm cannot serve yet, and
# raises until it can (huggorm#97).

#: The store operations a Python store may serve: none, until huggorm can
#: implement a store virtual in Python (huggorm#84).
STORE_DISPATCH_METHODS: tuple[str, ...] = ()


def register_store_implementation(scheme: str, factory: object) -> NoReturn:
    """Claim a URI scheme for a Python store. huggorm cannot serve one yet (huggorm#84)."""
    del scheme, factory
    raise NotImplementedError("huggorm cannot implement a Nix store in Python yet")
