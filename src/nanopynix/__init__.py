"""nanopynix — nanobind-based Python bindings for Nix."""

from nanopynix_expr import EvalState, Value, eval_file, init_libexpr, register_primop
from nanopynix_fetchers import input_from_attrs, input_from_url
from nanopynix_flake import get_flake, lock_flake, parse_flake_ref
from nanopynix_main import init_nix, init_plugins
from nanopynix_store import BuildMode, open_store
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

from nanopynix._pool import WorkerBusy, WorkerDied
from nanopynix._session import EvalSession, ValueAttrs, ValueList, ValueProxy
from nanopynix.exceptions import (
    AssertionError_,
    EvalError,
    EvalProxyError,
    EvalSessionClosedError,
    ForeignValueError,
    InfiniteRecursionError,
    MissingArgumentError,
    NixCoercionError,
    NixError,
    ParseError,
    RestrictedPathError,
    StoreError,
    ThrownError,
    TypeError_,
    UndefinedVarError,
    UnresolvedValueError,
    UsageError,
    ValueReleasedError,
    WrongNixTypeError,
)
from nanopynix.logging import LogCollector
from nanopynix.models import (
    BuildResult,
    Derivation,
    DerivationOutputs,
    FlakeRef,
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
from nanopynix.nix import LogCapture, Nix, Session
from nanopynix.primops import from_yaml, to_yaml, yaml_primops
from nanopynix.store import StoreHandle
from nanopynix.types import NixArg, NixDeepValue, NixValue

# Backward-compatible alias for StoreHandle
Store = StoreHandle

__all__ = [
    # L2
    "AssertionError_",
    "BuildMode",
    "BuildResult",
    "Derivation",
    "DerivationOutputs",
    "EvalError",
    "EvalProxyError",
    "EvalSessionClosedError",
    "EvalSession",
    # L1
    "EvalState",
    "FlakeRef",
    "ForeignValueError",
    "InfiniteRecursionError",
    "Input",
    "LockedFlake",
    "LockedInput",
    "LogCapture",
    "LogCollector",
    "LogEvent",
    "MissingArgumentError",
    "MissingInfo",
    "Nix",
    "NixArg",
    "NixCoercionError",
    "NixDeepValue",
    "NixError",
    "NixType",
    "NixValue",
    "ParseError",
    "PathInfo",
    "PrimOpSpec",
    "RestrictedPathError",
    "ResultType",
    "Session",
    "Store",
    "StoreError",
    "StoreHandle",
    "StorePath",
    "ThrownError",
    "TypeError_",
    "UndefinedVarError",
    "UnresolvedValueError",
    "UsageError",
    "Value",
    "ValueAttrs",
    "ValueHandle",
    "ValueList",
    "ValueProxy",
    "ValueReleasedError",
    "WorkerBusy",
    "WorkerDied",
    "WrongNixTypeError",
    "enable_experimental_feature",
    "eval_file",
    "from_yaml",
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
    "to_yaml",
    "yaml_primops",
]
