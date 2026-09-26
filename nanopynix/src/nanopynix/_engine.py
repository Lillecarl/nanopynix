"""The Nix engine, and the only module of nanopynix that imports it.

Every other module takes the engine from here. Two engines exist:
``nanopynix_bindings``, and huggorm's generated bindings, which are to replace
it. The venv decides, because two libnix copies cannot share one process, so
exactly one of the two packages must be installed.

The flat names are the engine surface that nanopynix itself calls or
publishes. The area modules are for the call sites that still reach into an
area of the bindings. ``_engine_huggorm`` answers the same names, and
``tests/meta/test_one_engine_module.py`` holds the two lists equal.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

#: The engine packages. The first is the default.
ENGINES = ("nanopynix_bindings", "huggorm_bindings")


def _installed_engine() -> str:
    found = [name for name in ENGINES if importlib.util.find_spec(name) is not None]
    if len(found) != 1:
        msg = f"nanopynix needs exactly one of {', '.join(ENGINES)} installed, and found {found or 'none'}"
        raise ImportError(msg)
    return found[0]


ENGINE = _installed_engine()

if TYPE_CHECKING or ENGINE == "nanopynix_bindings":
    from nanopynix_bindings import (
        errors as errors,
        expr as expr,
        fetchers as fetchers,
        flake as flake,
        signals as signals,
        store as store,
        util as util,
    )
    from nanopynix_bindings._get_env import get_env_sh_path as get_env_sh_path
    from nanopynix_bindings.expr import (
        EvalState as EvalState,
        PrimopError as PrimopError,
        Value as Value,
        eval_counters_enabled as eval_counters_enabled,
        eval_file as eval_file,
        init_libexpr as init_libexpr,
        is_pseudo_url as is_pseudo_url,
        register_primop as register_primop,
        set_eval_counters_enabled as set_eval_counters_enabled,
    )
    from nanopynix_bindings.fetchers import input_from_attrs as input_from_attrs, input_from_url as input_from_url
    from nanopynix_bindings.flake import (
        get_flake as get_flake,
        lock_flake as lock_flake,
        parse_flake_ref as parse_flake_ref,
    )
    from nanopynix_bindings.store import (
        STORE_DISPATCH_METHODS as STORE_DISPATCH_METHODS,
        BuildMode as BuildMode,
        Store as Store,
        open_store as open_store,
        process_connection as process_connection,
        register_store_implementation as register_store_implementation,
    )
    from nanopynix_bindings.util import (
        build_info as build_info,  # type: ignore[reportUnknownVariableType] -- C++ extension without type stubs
        current_system as current_system,
        enable_experimental_feature as enable_experimental_feature,
        filter_ansi_escapes as filter_ansi_escapes,
        get_verbosity as get_verbosity,
        init_libstore as init_libstore,
        install_logger as install_logger,
        list_settings as list_settings,
        remove_logger as remove_logger,
        set_verbosity as set_verbosity,
    )

else:
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
        errors as errors,
        eval_counters_enabled as eval_counters_enabled,
        eval_file as eval_file,
        expr as expr,
        fetchers as fetchers,
        filter_ansi_escapes as filter_ansi_escapes,
        flake as flake,
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
        set_verbosity as set_verbosity,
        signals as signals,
        store as store,
        util as util,
    )


def is_engine_error(exc: BaseException) -> bool:
    """Tell whether the engine raised *exc* for a Nix C++ exception.

    The class name of such an exception is the C++ name, so
    :func:`~nanopynix.exceptions.exception_for_nix_type` can look it up. The
    module decides, not the name: some Nix classes share a name with a Python
    builtin, such as ``TypeError``.
    """
    return type(exc).__module__.startswith(ENGINE)
