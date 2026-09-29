"""The Nix engine, and the only module of nanopynix that imports it.

Every other module takes the engine from here. The engine is huggorm's
generated bindings, and ``huggorm_bindings-stubs`` types each name.
"""

from __future__ import annotations

from typing import Final

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
    EvalState as EvalState,
    FlakeRef as FlakeRef,
    GCAction as GCAction,
    GCOptions as GCOptions,
    HashAlgorithm as HashAlgorithm,
    KeyedBuildResult as KeyedBuildResult,
    LockedFlake as LockedFlake,
    OutputsSpec as OutputsSpec,
    RegistryWrite as RegistryWrite,
    Repl as Repl,
    Store as Store,
    StorePath as StorePath,
    StoreReferenceAuto as StoreReferenceAuto,
    StoreReferenceDaemon as StoreReferenceDaemon,
    StoreReferenceLocal as StoreReferenceLocal,
    StoreReferenceSpecified as StoreReferenceSpecified,
    Value as Value,
    collect_garbage as collect_garbage,
    collector_owner_thread as collector_owner_thread,
    eval_settings_json as eval_settings_json,
    fetch_settings_json as fetch_settings_json,
    flake_settings_json as flake_settings_json,
    gc_release_thread as gc_release_thread,
    gc_stats as gc_stats,
    get_setting as get_setting,
    parse_flake_ref as parse_flake_ref,
    parse_nix_path as parse_nix_path,
    parse_store_reference as parse_store_reference,
    registry_add as registry_add,
    registry_entries as registry_entries,
    registry_pin as registry_pin,
    registry_remove as registry_remove,
    render_store_reference as render_store_reference,
    settings_json as settings_json,
    store_types_json as store_types_json,
    user_registry_path as user_registry_path,
)
from huggorm_bindings.errors import (  # pyright: ignore[reportMissingTypeStubs] -- errors.py ships as source, and huggorm_bindings-stubs has no errors.pyi
    BadStorePath as BadStorePath,
    EvalError as EvalError,
    NixError as NixError,
)

from nanopynix._engine_huggorm import (
    STORE_DISPATCH_METHODS as STORE_DISPATCH_METHODS,
    build_info as build_info,
    current_system as current_system,
    enable_experimental_feature as enable_experimental_feature,
    error_detail as error_detail,
    errors as errors,
    eval_counters_enabled as eval_counters_enabled,
    filter_ansi_escapes as filter_ansi_escapes,
    flush_logs as flush_logs,
    get_env_sh_path as get_env_sh_path,
    get_verbosity as get_verbosity,
    init_libexpr as init_libexpr,
    init_libstore as init_libstore,
    install_logger as install_logger,
    is_pseudo_url as is_pseudo_url,
    list_settings as list_settings,
    process_connection as process_connection,
    register_store_implementation as register_store_implementation,
    remove_logger as remove_logger,
    set_eval_counters_enabled as set_eval_counters_enabled,
    set_setting as set_setting,
    set_verbosity as set_verbosity,
    signals as signals,
    util as util,
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
