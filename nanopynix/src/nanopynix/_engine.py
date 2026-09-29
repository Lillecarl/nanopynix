"""The Nix engine, and the only module of nanopynix that imports it.

Every other module takes the engine from here. The engine is huggorm's
generated bindings, and ``_engine_huggorm`` gives them the names below.
"""

from __future__ import annotations

from nanopynix._engine_huggorm import (
    STORE_DISPATCH_METHODS as STORE_DISPATCH_METHODS,
    BuildMode as BuildMode,
    EvalState as EvalState,
    PrimopError as PrimopError,
    Store as Store,
    Value as Value,
    build_info as build_info,
    current_system as current_system,
    enable_experimental_feature as enable_experimental_feature,
    error_detail as error_detail,
    errors as errors,
    eval_counters_enabled as eval_counters_enabled,
    eval_file as eval_file,
    expr as expr,
    fetchers as fetchers,
    filter_ansi_escapes as filter_ansi_escapes,
    flake as flake,
    flush_logs as flush_logs,
    get_env_sh_path as get_env_sh_path,
    get_flake as get_flake,
    get_verbosity as get_verbosity,
    init_libexpr as init_libexpr,
    init_libstore as init_libstore,
    input_from_attrs as input_from_attrs,
    input_from_url as input_from_url,
    install_logger as install_logger,
    is_pseudo_url as is_pseudo_url,
    list_settings as list_settings,
    lock_flake as lock_flake,
    open_store as open_store,
    parse_flake_ref as parse_flake_ref,
    process_connection as process_connection,
    register_primop as register_primop,
    register_store_implementation as register_store_implementation,
    remove_logger as remove_logger,
    set_eval_counters_enabled as set_eval_counters_enabled,
    set_setting as set_setting,
    set_verbosity as set_verbosity,
    signals as signals,
    store as store,
    util as util,
)

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
