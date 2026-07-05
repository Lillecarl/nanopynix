"""nanopynix — nanobind-based Python bindings for Nix."""

# ── L2 facades (primary API) ────────────────────────────────────────
from nanopynix.exceptions import (
    AssertionError_,
    EvalError,
    InfiniteRecursionError,
    MissingArgumentError,
    NixError,
    ParseError,
    RestrictedPathError,
    StoreError,
    ThrownError,
    TypeError_,
    UndefinedVarError,
    UsageError,
)
from nanopynix.logging import LogCollector
from nanopynix.models import (
    BuildResult,
    Capture,
    FlakeRef,
    Input,
    LockedFlake,
    LockedInput,
    LogEvent,
    MissingInfo,
    PathInfo,
    ResultType,
    StorePath,
)
from nanopynix.nix import Nix, Session
from nanopynix._pool import WorkerDied
from nanopynix._session import EvalSession, ValueProxy
from nanopynix.store import Store

# ── L1 bindings (re-exported for power users) ───────────────────────
from nanopynix_expr import EvalState, Value, eval_file, init_libexpr, register_primop
from nanopynix_fetchers import Input as _L1Input, input_from_attrs, input_from_url
from nanopynix_flake import FlakeRef as _L1FlakeRef, LockedFlake as _L1LockedFlake
from nanopynix_flake import get_flake, lock_flake, parse_flake_ref
from nanopynix_main import init_nix, init_plugins
from nanopynix_store import BuildMode, Store as _L1Store, open_store
from nanopynix_util import (
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
    # L2
    "BuildMode",
    "BuildResult",
    "Capture",
    "AssertionError_",
    "EvalError",
    "EvalSession",
    "FlakeRef",
    "InfiniteRecursionError",
    "Input",
    "LockedFlake",
    "LockedInput",
    "LogEvent",
    "MissingArgumentError",
    "MissingInfo",
    "LogCollector",
    "Nix",
    "NixError",
    "ParseError",
    "RestrictedPathError",
    "StoreError",
    "ThrownError",
    "TypeError_",
    "UndefinedVarError",
    "UsageError",
    "ValueProxy",
    "WorkerDied",
    "PathInfo",
    "ResultType",
    "Session",
    "Store",
    "StorePath",
    # L1
    "EvalState",
    "Value",
    "enable_experimental_feature",
    "eval_file",
    "get_flake",
    "get_setting",
    "get_verbosity",
    "init_libexpr",
    "init_libstore",
    "init_nix",
    "init_plugins",
    "input_from_attrs",
    "input_from_url",
    "install_logger",
    "list_settings",
    "lock_flake",
    "open_store",
    "parse_flake_ref",
    "register_primop",
    "remove_logger",
    "set_setting",
    "set_verbosity",
]
