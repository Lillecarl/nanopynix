"""The huggorm engine: nanopynix's engine surface, answered by huggorm's bindings.

A port in progress, and huggorm's ``tasks/097`` tracks it. Each name that is
not ported yet is a placeholder, so ``import nanopynix`` works and each test
fails where it reaches the gap, with an error that names it. The failures of
the huggorm lane are then the list of what is left, one name at a time.

A placeholder goes when its name is ported. None may stay once the lane is
green: a caller that reaches one gets ``NotImplementedError``.

Only a venv that installs ``huggorm_bindings`` and not ``nanopynix_bindings``
imports this module, and ``nanopynix._engine`` decides that.
"""

from __future__ import annotations

from typing import Any, NoReturn

import huggorm_bindings  # type: ignore[reportMissingImports] -- installed only in the huggorm scope


class NotPortedError(NotImplementedError):
    """The huggorm engine does not answer this name yet."""


class _NotPortedMeta(type):
    """The metaclass of a name the huggorm engine does not answer yet.

    A placeholder is a class and not an instance, because nanopynix uses these
    names in annotations (``BuildMode | int``) and in ``except`` clauses, and
    both need a type. An attribute of a placeholder is another placeholder, so
    an area such as ``expr`` stands in for all of its names, and the error
    names the whole path.
    """

    def __getattr__(cls, attr: str) -> type:
        if attr.startswith("__"):
            raise AttributeError(attr)
        return _not_ported(f"{cls.__qualname__}.{attr}")

    def __call__(cls, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise NotPortedError(f"the huggorm engine has no {cls.__qualname__} yet (huggorm tasks/097)")


def _not_ported(name: str) -> type:
    """A placeholder class for *name*. ``Exception`` is its base, so it can stand in an ``except``."""
    return _NotPortedMeta(name.rsplit(".", 1)[-1], (Exception,), {"__qualname__": name, "__module__": __name__})


#: Loaded, so that a scope whose extension does not link fails at import.
ENGINE_MODULE = huggorm_bindings

errors = _not_ported("errors")
expr = _not_ported("expr")
fetchers = _not_ported("fetchers")
flake = _not_ported("flake")
signals = _not_ported("signals")
store = _not_ported("store")
util = _not_ported("util")

get_env_sh_path = _not_ported("get_env_sh_path")

EvalState = _not_ported("EvalState")
PrimopError = _not_ported("PrimopError")
Value = _not_ported("Value")
eval_counters_enabled = _not_ported("eval_counters_enabled")
eval_file = _not_ported("eval_file")
init_libexpr = _not_ported("init_libexpr")
is_pseudo_url = _not_ported("is_pseudo_url")
register_primop = _not_ported("register_primop")
set_eval_counters_enabled = _not_ported("set_eval_counters_enabled")

input_from_attrs = _not_ported("input_from_attrs")
input_from_url = _not_ported("input_from_url")

get_flake = _not_ported("get_flake")
lock_flake = _not_ported("lock_flake")
parse_flake_ref = _not_ported("parse_flake_ref")

STORE_DISPATCH_METHODS: tuple[str, ...] = ()
BuildMode = _not_ported("BuildMode")
Store = _not_ported("Store")
open_store = _not_ported("open_store")
process_connection = _not_ported("process_connection")
register_store_implementation = _not_ported("register_store_implementation")

build_info = _not_ported("build_info")
current_system = _not_ported("current_system")
enable_experimental_feature = _not_ported("enable_experimental_feature")
filter_ansi_escapes = _not_ported("filter_ansi_escapes")
get_verbosity = _not_ported("get_verbosity")
init_libstore = _not_ported("init_libstore")
install_logger = _not_ported("install_logger")
list_settings = _not_ported("list_settings")
remove_logger = _not_ported("remove_logger")
set_verbosity = _not_ported("set_verbosity")
