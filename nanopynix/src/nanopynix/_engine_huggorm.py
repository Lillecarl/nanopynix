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

import os
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


def _not_ported(name: str, **ported: object) -> type:
    """A placeholder class for *name*. ``Exception`` is its base, so it can stand in an ``except``.

    *ported* are the names it does answer: an area of the bindings, such as
    ``util``, answers those and is a placeholder for the rest.
    """
    namespace = {"__qualname__": name, "__module__": __name__, **ported}
    return _NotPortedMeta(name.rsplit(".", 1)[-1], (Exception,), namespace)


#: Loaded, so that a scope whose extension does not link fails at import.
ENGINE_MODULE = huggorm_bindings

errors = _not_ported("errors")
fetchers = _not_ported("fetchers")
flake = _not_ported("flake")
signals = _not_ported("signals")

get_env_sh_path = _not_ported("get_env_sh_path")

EvalState = _not_ported("EvalState")
PrimopError = _not_ported("PrimopError")
Value = _not_ported("Value")
eval_counters_enabled = _not_ported("eval_counters_enabled")
eval_file = _not_ported("eval_file")
init_libexpr = _not_ported("init_libexpr")
is_pseudo_url = huggorm_bindings.is_pseudo_url
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

current_system = huggorm_bindings.current_system
set_setting = huggorm_bindings.set_setting


def build_info() -> dict[str, Any]:
    """The linked Nix's version, and what this engine can do.

    ``boehm_gc`` is a fact about the linked libexpr, and huggorm answers it.
    The rest are nanopynix features the huggorm engine does not offer yet,
    so each is ``False`` until its name is ported. They are here and not
    absent, because callers index them.
    """
    return {
        "nix_version": huggorm_bindings.nix_version(),
        "capabilities": {
            "boehm_gc": huggorm_bindings.boehm_gc(),
            "dynamic_primop_registration": False,
            "eval_statistics": False,
            "store_impl_read_derivation": False,
        },
    }


def enable_experimental_feature(name: str) -> None:
    """Add *name* to the enabled features, as ``extra-experimental-features`` does in nix.conf."""
    huggorm_bindings.set_setting("extra-experimental-features", name)


filter_ansi_escapes = huggorm_bindings.filter_ansi_escapes
get_verbosity = _not_ported("get_verbosity")


_config_loaded = False


def init_libstore(load_config: bool = True) -> None:
    """Read nix.conf once, as ``nix::initLibStore`` does in the other engine.

    huggorm initialises libstore at import and reads no configuration, so
    all that is left is the file. The first call that asks reads it, and a
    later call changes nothing.
    """
    global _config_loaded  # noqa: PLW0603 -- process-wide, like the Nix state it mirrors
    if load_config and not _config_loaded:
        huggorm_bindings.load_config()
        _config_loaded = True


install_logger = _not_ported("install_logger")
list_settings = huggorm_bindings.list_settings
remove_logger = _not_ported("remove_logger")
set_verbosity = _not_ported("set_verbosity")


def parse_nix_path(value: str | None = None) -> list[str]:
    """Split a search path as Nix does; ``None`` reads ``NIX_PATH``."""
    raw = os.environ.get("NIX_PATH", "") if value is None else value
    return list(huggorm_bindings.parse_nix_path(raw)) if raw else []


expr = _not_ported("expr", is_pseudo_url=is_pseudo_url, parse_nix_path=parse_nix_path)
store = _not_ported("store", render_store_reference=huggorm_bindings.render_store_reference)


util = _not_ported(
    "util",
    build_info=build_info,
    current_system=current_system,
    enable_experimental_feature=enable_experimental_feature,
    get_setting=huggorm_bindings.get_setting,
    init_libstore=init_libstore,
    list_settings=huggorm_bindings.list_settings,
    list_settings_metadata_json=huggorm_bindings.settings_json,
    reset_overridden=huggorm_bindings.reset_overridden,
    set_setting=set_setting,
)
