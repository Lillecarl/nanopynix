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

import dataclasses
import enum
import importlib.resources
import inspect
import itertools
import json
import logging
import os
import threading
import time
import types
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import huggorm_bindings  # type: ignore[reportMissingImports] -- installed only in the huggorm scope
from huggorm_bindings import errors as huggorm_errors  # type: ignore[reportMissingImports] -- as above
from huggorm_bindings.errors import BadStorePath, EvalError, NixError  # type: ignore[reportMissingImports] -- as above
from nanopynix_proto.nix.common import ActivityType, LogLevel, ResultType

from nanopynix._typechecking import BEARTYPING
from nanopynix._wire import NO_GC_LIMIT

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable


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

# The other engine's names for huggorm's classes. Three differ: huggorm
# spells Nix's `TypeError` and `AssertionError` with `Nix` in front, so
# they do not shadow the builtins, calls `Interrupted` what the other
# engine calls `OperationCancelled`, and calls the root `NixError`.
#
# Every name of the other engine's module, so a plain namespace: a name
# it lacks raises AttributeError, as that module does.
errors = types.SimpleNamespace(
    Error=NixError,
    EvalBaseError=huggorm_errors.EvalBaseError,
    EvalError=EvalError,
    ParseError=huggorm_errors.ParseError,
    TypeError=huggorm_errors.NixTypeError,
    UndefinedVarError=huggorm_errors.UndefinedVarError,
    AssertionError=huggorm_errors.NixAssertionError,
    ThrownError=huggorm_errors.ThrownError,
    InvalidPath=huggorm_errors.InvalidPath,
    Unsupported=huggorm_errors.Unsupported,
    BadStorePath=BadStorePath,
    SysError=huggorm_errors.SysError,
    UsageError=huggorm_errors.UsageError,
    UnimplementedError=huggorm_errors.UnimplementedError,
    MissingAttributeError=huggorm_errors.MissingAttribute,
    ListIndexError=huggorm_errors.ListIndex,
    OperationCancelled=huggorm_errors.Interrupted,
)
_scope_ids = itertools.count(1)


class InterruptToken:
    """A cancel that one thread arms with :class:`interrupt_scope` and any thread sets.

    It owns one huggorm interrupt scope. huggorm keeps a cancelled scope in a
    table until it is forgotten, and every ``checkInterrupt`` in the process
    takes a lock while that table is not empty. So the token forgets its
    scope when the scope ends, and a cancel that arrives later records only
    the flag.
    """

    def __init__(self) -> None:
        self._scope = next(_scope_ids)
        self._lock = threading.Lock()
        self._cancelled = False
        self._ended = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if not self._ended:
                huggorm_bindings.cancel_interrupt_scope(self._scope)

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False
            self._ended = False
            huggorm_bindings.forget_interrupt_scope(self._scope)

    def arm(self) -> int:
        """Arm this token on the calling thread. Answers the scope it replaced."""
        return huggorm_bindings.begin_interrupt_scope(self._scope)

    def disarm(self, previous: int) -> None:
        """Restore *previous* on the calling thread, and forget this scope."""
        huggorm_bindings.end_interrupt_scope(previous)
        with self._lock:
            self._ended = True
            huggorm_bindings.forget_interrupt_scope(self._scope)


class interrupt_scope:  # noqa: N801 -- the other engine's name for the same context manager
    """Arm *token* on this thread while the block runs."""

    def __init__(self, token: InterruptToken) -> None:
        self._token = token
        self._previous: int | None = None

    def __enter__(self) -> interrupt_scope:
        if self._previous is not None:
            raise ValueError("interrupt scope is already active")
        self._previous = self._token.arm()
        return self

    def __exit__(self, *_exc: object) -> None:
        previous, self._previous = self._previous, None
        if previous is not None:
            self._token.disarm(previous)


signals = _not_ported("signals", InterruptToken=InterruptToken, interrupt_scope=interrupt_scope)


def get_env_sh_path() -> Path:
    """Return Nix's ``get-env.sh``, which ``huggorm_bindings`` carries."""
    return Path(str(importlib.resources.files(huggorm_bindings) / "get-env.sh"))


eval_counters_enabled = _not_ported("eval_counters_enabled")


def init_libexpr() -> None:
    """Start the collector and enable ``fetch-tree``, as the other engine's ``init_libexpr`` does.

    huggorm starts the collector at its first evaluator by itself. Starting it
    here keeps the other engine's order: Nix copies ``NIX_PATH`` into
    ``nix-path`` as the session starts, and not during some later test.
    """
    huggorm_bindings.start_collector()
    enable_experimental_feature("fetch-tree")


is_pseudo_url = huggorm_bindings.is_pseudo_url
set_eval_counters_enabled = _not_ported("set_eval_counters_enabled")

input_from_attrs = _not_ported("input_from_attrs")
input_from_url = _not_ported("input_from_url")


STORE_DISPATCH_METHODS: tuple[str, ...] = ()


class BuildMode(enum.IntEnum):
    """Nix's build modes, by the names and integers the other engine uses."""

    Normal = 0
    Repair = 1
    Check = 2

    def huggorm(self) -> Any:
        return huggorm_bindings.BuildMode[self.name.upper()]


class GCAction(enum.Enum):
    """Nix's collector actions, by the names the other engine uses."""

    ReturnLive = huggorm_bindings.GCAction.RETURN_LIVE
    ReturnDead = huggorm_bindings.GCAction.RETURN_DEAD
    DeleteDead = huggorm_bindings.GCAction.DELETE_DEAD
    DeleteSpecific = huggorm_bindings.GCAction.DELETE_SPECIFIC


def _build_result(result: Any, store: Any) -> dict[str, Any]:
    """A huggorm ``KeyedBuildResult`` as the other engine's dict: paths absolute, statuses as words."""
    target = result.path()
    rendered = store.print_derived_path(target)
    if isinstance(target, huggorm_bindings.DerivedPathBuilt):
        spec = target.outputs()
        drv_path, outputs = rendered.rsplit("^", 1)[0], ["*"] if spec.all() else spec.names()
    else:
        drv_path, outputs = rendered, []
    prefix = f"{store.store_dir()}/"
    success, error = result.success(), result.error()
    if success is not None:
        status, error_msg = str(success.status()), ""
        built_outputs = {
            name: {
                "out_path": prefix + realisation.out_path().to_string(),
                "signatures": [signature.to_string() for signature in realisation.signatures()],
            }
            for name, realisation in success.built_outputs().items()
        }
    elif error is not None:
        # `colored` is Nix's message as it came; `message` strips it.
        status, error_msg, built_outputs = str(error.status), error.colored, {}
    else:
        status, error_msg, built_outputs = "unknown", "", {}
    return {
        "drv_path": drv_path,
        "outputs": outputs,
        "success": success is not None,
        "status": status,
        "error_msg": error_msg,
        "built_outputs": built_outputs,
    }


class _PathInfo:
    """A huggorm ``PathInfo`` in the other engine's shape: strings, and paths absolute."""

    __slots__ = ("ca", "deriver", "nar_hash", "nar_size", "path", "references", "registration_time", "sigs", "ultimate")

    def __init__(self, info: Any) -> None:
        prefix = f"{info.store_dir()}/"
        deriver = info.deriver()
        ca = info.ca()
        self.path: str = prefix + info.path().to_string()
        self.references: list[str] = [prefix + reference.to_string() for reference in info.references()]
        self.nar_hash: str = info.nar_hash().sri()
        self.nar_size: int = info.nar_size()
        # The other engine reports Nix's unset time, 0, as None.
        self.registration_time: int | None = info.registration_time() or None
        self.deriver: str | None = None if deriver is None else prefix + deriver.to_string()
        self.ca: str | None = None if ca is None else ca.render()
        self.ultimate: bool = info.ultimate()
        self.sigs: list[str] = [signature.to_string() for signature in info.sigs()]


class _MissingPaths:
    """A huggorm ``MissingPaths`` in the other engine's shape: paths absolute."""

    __slots__ = ("download_size", "nar_size", "unknown", "will_build", "will_substitute")

    def __init__(self, missing: Any, prefix: str) -> None:
        self.will_build: list[str] = [prefix + path.to_string() for path in missing.will_build()]
        self.will_substitute: list[str] = [prefix + path.to_string() for path in missing.will_substitute()]
        self.unknown: list[str] = [prefix + path.to_string() for path in missing.unknown()]
        self.download_size: int = missing.download_size()
        self.nar_size: int = missing.nar_size()


class _InputDrvNode:
    """A huggorm ``InputDrvNode`` in the other engine's shape: fields, not methods."""

    __slots__ = ("dynamic_outputs", "outputs")

    def __init__(self, node: Any) -> None:
        self.outputs: list[str] = node.outputs()
        self.dynamic_outputs: dict[str, _InputDrvNode] = {
            name: _InputDrvNode(child) for name, child in node.dynamic_outputs().items()
        }


class _DerivationOutput:
    """One arm of a huggorm ``DerivationOutput``, flattened to the other engine's five fields."""

    __slots__ = ("ca", "hash_algo", "method", "path", "type")

    def __init__(self, output: Any, prefix: str) -> None:
        self.path: str | None = None
        self.ca: str | None = None
        self.method: str | None = None
        self.hash_algo: str | None = None
        match output:
            case huggorm_bindings.DerivationOutputInputAddressed():
                self.type = "InputAddressed"
                self.path = prefix + output.path().to_string()
            case huggorm_bindings.DerivationOutputCAFixed():
                self.type = "CAFixed"
                self.ca = output.ca().render()
            case huggorm_bindings.DerivationOutputCAFloating():
                self.type = "CAFloating"
                self.method, self.hash_algo = str(output.method()), str(output.hash_algo())
            case huggorm_bindings.DerivationOutputDeferred():
                self.type = "Deferred"
            case huggorm_bindings.DerivationOutputImpure():
                self.type = "Impure"
                self.method, self.hash_algo = str(output.method()), str(output.hash_algo())
            case _:
                raise TypeError(f"huggorm returned an unknown derivation output: {output!r}")


class _Derivation:
    """A huggorm ``Derivation`` in the other engine's shape: strings, and paths absolute."""

    __slots__ = ("args", "builder", "env", "input_drvs", "input_srcs", "name", "outputs", "structured_attrs", "system")

    def __init__(self, drv: Any, store_dir: str) -> None:
        prefix = f"{store_dir}/"
        self.name: str = drv.name()
        self.system: str = drv.system()
        self.builder: str = drv.builder()
        self.args: list[str] = drv.args()
        self.env: dict[str, str] = drv.env()
        self.input_srcs: list[str] = [prefix + path.to_string() for path in drv.input_srcs()]
        self.input_drvs: dict[str, _InputDrvNode] = {
            prefix + base_name: _InputDrvNode(node) for base_name, node in drv.input_drvs().items()
        }
        self.outputs: dict[str, _DerivationOutput] = {
            name: _DerivationOutput(output, prefix) for name, output in drv.outputs().items()
        }
        self.structured_attrs: str | None = drv.structured_attrs()


def _added_name(name: str | None, path: str) -> str:
    """The name an added path gets: *name*, else the last component of *path*.

    ``os.path.basename`` and not ``Path.name``: a trailing slash leaves no
    name, as it does in the other engine, and Nix refuses the empty one.
    """
    return os.path.basename(path) if name is None else name  # noqa: PTH119 -- Path.name drops a trailing slash


class Store:
    """A huggorm ``Store`` that answers the other engine's method names.

    ``nanopynix._core._objects.CoreStore`` is the one caller, so its calls are
    the whole surface. A method not ported yet is a placeholder, and calling it
    raises :class:`NotPortedError` naming ``Store.<method>``.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        return _not_ported(f"Store.{name}")

    def close(self) -> None:
        self.store.close()

    def get_store_dir(self) -> str:
        return self.store.store_dir()

    def get_uri(self, *, with_params: bool = False) -> str:
        return self.store.reference() if with_params else self.store.get_uri()

    def parse_store_path(self, path: str) -> Any:
        return self.store.parse_store_path(path)

    def is_valid_path(self, path: Any) -> bool:
        return self.store.is_valid_path(path)

    def query_path_info_typed(self, path: Any) -> _PathInfo:
        return _PathInfo(self.store.query_path_info(path))

    def query_path_info(self, path: Any) -> dict[str, Any]:
        info = self.query_path_info_typed(path)
        return {name: getattr(info, name) for name in _PathInfo.__slots__}

    def follow_links_to_store_path(self, path: str) -> Any:
        return self.store.follow_links_to_store_path(path)

    def query_path_from_hash_part(self, hash_part: str) -> Any:
        return self.store.query_path_from_hash_part(hash_part)

    def query_all_valid_paths(self) -> list[str]:
        # Printed, as the other engine answers.
        return [self.store.print_store_path(path) for path in self.store.query_all_valid_paths()]

    def compute_fs_closure(
        self, path: Any, flip_direction: bool, include_outputs: bool, include_derivers: bool
    ) -> list[Any]:
        return self.store.compute_fs_closure([path], flip_direction, include_outputs, include_derivers)

    def query_derivation_outputs(self, path: Any) -> list[Any]:
        return list(self.store.query_derivation_output_map(path).values())

    def query_valid_derivers(self, path: Any) -> list[Any]:
        return self.store.query_valid_derivers(path)

    def query_referrers(self, path: Any) -> list[Any]:
        return self.store.query_referrers(path)

    def query_substitutable_paths(self, paths: list[Any]) -> list[Any]:
        return self.store.query_substitutable_paths(paths)

    def get_build_log(self, path: Any) -> str | None:
        return self.store.get_build_log(path)

    def dump_db(self, paths: list[Any], show_derivers: bool, show_hash: bool) -> str:
        # One path at a time: Nix takes a set, and the other engine keeps the caller's order.
        return "".join(self.store.make_validity_registration([path], show_derivers, show_hash) for path in paths)

    def get_store_dirs(self) -> dict[str, str | None]:
        def text(path: Any) -> str | None:
            return None if path is None else str(path)

        return {
            "store_dir": self.store.store_dir(),
            "uri": self.store.get_uri(),
            "root_dir": text(self.store.root_dir()),
            "state_dir": text(self.store.state_dir()),
            "log_dir": text(self.store.log_dir()),
            "real_store_dir": text(self.store.real_store_dir()),
            "build_dir": text(self.store.build_dir()),
        }

    def add_perm_root(self, path: Any, gc_root: str) -> str:
        return str(self.store.add_perm_root(path, gc_root))

    def optimise_store(self) -> None:
        self.store.optimise_store()

    def verify_store(self, check_contents: bool, repair: bool) -> bool:
        return self.store.verify_store(check_contents, repair)

    def add_indirect_root(self, path: str) -> None:
        self.store.add_indirect_root(path)

    def find_roots(self, censor: bool) -> list[dict[str, str]]:
        return [
            {"link": root.link(), "path": self.store.print_store_path(root.path())}
            for root in self.store.find_roots(censor)
        ]

    def collect_garbage(
        self, action: GCAction, ignore_liveness: bool, paths_to_delete: list[Any], max_freed: int
    ) -> dict[str, Any]:
        options = huggorm_bindings.GCOptions(
            action.value,
            ignore_liveness,
            paths_to_delete,
            # huggorm spells Nix's "no limit" as None, not as the largest u64.
            None if max_freed == NO_GC_LIMIT else max_freed,
        )
        results = self.store.collect_garbage(options)
        return {"paths": list(results.paths()), "bytes_freed": results.bytes_freed()}

    def query_missing_typed(self, paths: list[Any]) -> _MissingPaths:
        return _MissingPaths(self.store.query_missing(self._derived_paths(paths)), f"{self.store.store_dir()}/")

    def _derived_paths(self, paths: list[Any]) -> list[Any]:
        prefix = f"{self.store.store_dir()}/"
        # The other engine takes a `StorePath` or a string, and reads a base
        # name as a path in this store.
        texts = [path if isinstance(path, str) else path.to_string() for path in paths]
        return [self.store.parse_derived_path(text if text.startswith("/") else prefix + text) for text in texts]

    def build_paths_with_results(
        self, paths: list[str], build_mode: int, eval_store: Store | None
    ) -> list[dict[str, Any]]:
        results = self.store.build_paths_with_results(
            self._derived_paths(paths),
            BuildMode(build_mode).huggorm(),
            None if eval_store is None else eval_store.store,
        )
        return [_build_result(result, self.store) for result in results]

    def query_missing(self, paths: list[Any]) -> dict[str, Any]:
        missing = self.query_missing_typed(paths)
        return {name: getattr(missing, name) for name in _MissingPaths.__slots__}

    def read_derivation_typed(self, drv_path: Any) -> _Derivation:
        return _Derivation(self.store.read_derivation(drv_path), self.store.store_dir())

    def write_dev_shell_derivation(self, drv_path: Any, get_env_script: str) -> Any:
        """``getDerivationEnvironment`` in Nix's ``develop.cc``, over the derivation's JSON.

        ``add_derivation`` fills in the deferred output paths, so no Nix version
        branch and no hash computation happens here.
        """
        document = json.loads(self.store.read_derivation(drv_path).to_json())
        if os.path.basename(document["builder"]) != "bash":  # noqa: PTH119 -- Nix's baseNameOf, on a string
            raise NixError("'develop' only works on derivations that use 'bash' as their builder")
        script = self.store.add_to_store(
            "get-env.sh",
            get_env_script.encode(),
            huggorm_bindings.ContentAddressMethod.TEXT,
            huggorm_bindings.HashAlgorithm.SHA256,
        )
        document["args"] = [self.store.print_store_path(script)]
        # A dev shell is not the build, so the build's reference checks do not apply.
        if document.get("structuredAttrs") is not None:
            document["structuredAttrs"].pop("outputChecks", None)
        else:
            for check in ("allowedReferences", "allowedRequisites", "disallowedReferences", "disallowedRequisites"):
                document["env"].pop(check, None)
        document["name"] += "-env"
        document["env"]["name"] = document["name"]
        document["inputs"]["srcs"].append(script.to_string())
        for name, output in document["outputs"].items():
            # Input-addressed and fixed outputs have a path to invalidate; the other kinds have none.
            if "path" in output or "hash" in output:
                document["outputs"][name] = {}
                document["env"][name] = ""
        return self.store.add_derivation(json.dumps(document))

    def ensure_path(self, path: Any) -> None:
        self.store.ensure_path(path)

    def add_to_store(self, path: str, name: str | None = None, method: str = "nar", hash_algo: str = "sha256") -> Any:
        return self.store.add_path_to_store(_added_name(name, path), path, method, hash_algo)

    def compute_store_path(
        self, path: str, name: str | None = None, method: str = "nar", hash_algo: str = "sha256"
    ) -> Any:
        return self.store.compute_store_path(_added_name(name, path), path, method, hash_algo)

    def add_temp_root(self, path: Any) -> None:
        self.store.add_temp_root(path)

    def copy_closure(
        self, paths: list[Any], destination: Store, repair: bool, check_sigs: bool, substitute: bool
    ) -> None:
        self.store.copy_closure(destination.store, paths, repair, check_sigs, substitute)


def open_store(uri: str) -> Store:
    return Store(huggorm_bindings.Store(uri))


def parse_store_reference(uri: str) -> dict[str, Any]:
    """A store URI as libstore reads it, in the dict the other engine returns."""
    reference = huggorm_bindings.parse_store_reference(uri)
    match reference.variant():
        case huggorm_bindings.StoreReferenceAuto():
            kind, scheme, authority = "Auto", None, None
        case huggorm_bindings.StoreReferenceDaemon():
            kind, scheme, authority = "Daemon", "unix", ""
        case huggorm_bindings.StoreReferenceLocal():
            kind, scheme, authority = "Local", "local", ""
        case specified:
            kind, scheme, authority = "Specified", specified.scheme(), specified.authority()
    return {
        "type": kind,
        "scheme": scheme,
        "authority": authority,
        "params": reference.params(),
        "render": reference.render(),
        "render_without_params": reference.render(with_params=False),
    }


def _registry_write(wrote: Any) -> dict[str, Any]:
    return {"path": wrote.path(), "removed": wrote.removed(), "to": wrote.target(), "locked": wrote.locked()}


def _list_registry_entries(store: Store, fetch_settings: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "type": str(entry.layer()),
            "from": entry.source(),
            "to": entry.target(),
            "exact": entry.exact(),
            "extra_attrs": entry.extra_attrs(),
        }
        for entry in huggorm_bindings.registry_entries(store.store, fetch_settings)
    ]


def _registry_add(path: str, from_url: str, to_url: str, fetch_settings: dict[str, str]) -> dict[str, Any]:
    return _registry_write(huggorm_bindings.registry_add(path or None, from_url, to_url, settings=fetch_settings))


def _registry_remove(path: str, from_url: str, fetch_settings: dict[str, str]) -> dict[str, Any]:
    return _registry_write(huggorm_bindings.registry_remove(path or None, from_url, settings=fetch_settings))


def _registry_pin(store: Store, path: str, url: str, locked_url: str, fetch_settings: dict[str, str]) -> dict[str, Any]:
    return _registry_write(
        huggorm_bindings.registry_pin(store.store, path or None, url, locked_url or None, settings=fetch_settings)
    )


fetchers = _not_ported(
    "fetchers",
    list_fetch_settings_metadata_json=huggorm_bindings.fetch_settings_json,
    list_registry_entries=_list_registry_entries,
    user_registry_path=huggorm_bindings.user_registry_path,
    registry_add=_registry_add,
    registry_remove=_registry_remove,
    registry_pin=_registry_pin,
)


class PrimopError(Exception):
    """A primop's own failure, whose message Nix shows as it is."""


# Registered before a state opens, and given to each state that opens after,
# as the other engine's global `RegisterPrimOp` is.
_primops: dict[str, tuple[int, Callable[..., Any]]] = {}


def register_primop(name: str, arity: int, arg_names: list[str], doc: str, callback: Callable[..., Any]) -> None:
    """Give every state opened after this call ``builtins.<name>``.

    ``arg_names`` and ``doc`` are not kept: huggorm's primop carries neither.
    """
    del arg_names, doc
    _primops[name] = (arity, callback)


def _cleanup_primop_registry() -> None:
    _primops.clear()


def _sleep(seconds: object) -> bool:
    """``builtins.sleep``, which the other engine adds in C++ for tests.

    It polls no interrupt while it waits, so a cancellation cannot stop it.
    That is what the cancel tests need.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise PrimopError(f"builtins.sleep takes a number of seconds, got {seconds!r}")
    if seconds < 0:
        raise PrimopError(f"builtins.sleep takes a number of seconds that is not negative, got {seconds:f}")
    time.sleep(seconds)
    return True


#: What ``to_json`` refuses, by huggorm's type name.
_NOT_JSON = frozenset({"function", "external"})


def _primop_argument(state: Any, primop: str, value: Any) -> object:
    """A primop's argument as JSON data, or the other engine's error naming what JSON cannot hold."""
    try:
        return json.loads(value.to_json(False))
    except NixError as error:
        try:
            kind = _first_non_json(state, value)
        except NixError:
            kind = None
        if kind is None:
            raise
        raise EvalError(f"{primop}: argument contains non JSON-compatible Nix value of type '{kind}'") from error


def _first_non_json(state: Any, value: Any) -> str | None:
    state.force(value)
    kind = value.type_name()
    if kind in _NOT_JSON:
        return kind
    if kind == "list":
        items = (value.at(index) for index in range(value.length()))
    elif kind == "attrs":
        items = (value.get(name) for name in value.names())
    else:
        return None
    return next((found for item in items if (found := _first_non_json(state, item)) is not None), None)


# `bool` before `int`, because a bool is an int to `isinstance`.
_SCALAR_MAKERS = ((bool, "make_bool"), (int, "make_int"), (float, "make_float"), (str, "make_string"))


class Value:
    """A huggorm ``Value`` that answers the other engine's method names.

    ``nanopynix._core._objects.CoreValue`` is the one caller. The other
    engine forces a value before it reads one, and huggorm refuses to read
    a thunk, so each read forces first.
    """

    __slots__ = ("_state", "raw")

    def __init__(self, state: EvalState, value: Any) -> None:
        self._state = state
        # Any, so a released value can hold None.
        self.raw: Any = value

    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        return _not_ported(f"Value.{name}")

    def _forced(self) -> Any:
        self._state.state.force(self.raw)
        return self.raw

    def _child(self, value: Any) -> Value:
        self._state.state.force(value)
        return Value(self._state, value)

    def _release(self) -> None:
        self.raw = None

    def force(self) -> None:
        self._forced()

    def to_python(self) -> Any:
        return json.loads(self._forced().to_json(False))

    def to_json(self, *, copy_to_store: bool = False) -> Any:
        return json.loads(self._forced().to_json(copy_to_store))

    def type_name(self) -> str:
        return self.raw.type_name()

    # Without forcing, as the other engine's predicates are: a thunk
    # answers False to each of them and True to `is_thunk`.
    def is_null(self) -> bool:
        return self.raw.type_name() == "null"

    def is_int(self) -> bool:
        return self.raw.type_name() == "int"

    def is_float(self) -> bool:
        return self.raw.type_name() == "float"

    def is_bool(self) -> bool:
        return self.raw.type_name() == "bool"

    def is_string(self) -> bool:
        return self.raw.type_name() == "string"

    def is_path(self) -> bool:
        return self.raw.type_name() == "path"

    def is_attrs(self) -> bool:
        return self.raw.type_name() == "attrs"

    def is_list(self) -> bool:
        return self.raw.type_name() == "list"

    def is_function(self) -> bool:
        return self.raw.type_name() == "function"

    def is_thunk(self) -> bool:
        return self.raw.type_name() == "thunk"

    def as_int(self) -> int:
        return self._forced().integer()

    def as_float(self) -> float:
        # Nix's forceFloat widens an integer; huggorm's accessor reads one kind.
        value = self._forced()
        if value.type_name() == "int":
            return float(value.integer())
        return value.floating()

    def as_bool(self) -> bool:
        return self._forced().boolean()

    def as_string(self) -> str:
        return self._forced().string_value()

    def realise_string(self) -> str:
        return self._forced().realise_string()

    def realise_argv(self) -> list[str]:
        return self._forced().realise_argv()

    def get_doc(self) -> dict[str, Any] | None:
        doc = self.raw.doc()
        if doc is None:
            return None
        return {
            "name": doc.name(),
            "args": doc.args(),
            "arity": doc.arity(),
            "doc": doc.doc(),
            "path": doc.path(),
            "line": doc.line(),
        }

    def attr_doc(self, name: str) -> dict[str, Any] | None:
        doc = self.raw.attr_doc(name)
        if doc is None:
            return None
        return {"path": doc.path(), "line": doc.line(), "doc": doc.doc()}

    def edit_location(self) -> dict[str, Any]:
        location = self.raw.edit_location()
        return {"path": location.path(), "line": location.line()}

    def attr_get(self, name: str) -> Value:
        return self._child(self._forced().get(name))

    def has_attr(self, name: str) -> bool:
        return self._forced().has(name)

    def attr_names(self) -> list[str]:
        return self._forced().names()

    def list_get(self, index: int) -> Value:
        return self._child(self._forced().at(index))

    def list_length(self) -> int:
        return self._forced().length()

    def call(self, argument: Value) -> Value:
        return self._child(self._forced().apply(argument.raw))

    def auto_call(self) -> Value:
        """Apply with no arguments, as Nix's ``autoCallFunction`` does.

        That fills a lambda's defaulted formals, and follows ``__functor``. It
        answers anything else unapplied, where huggorm's ``apply_auto`` refuses.
        """
        value = self._forced()
        if value.type_name() == "attrs" and value.has("__functor"):
            functor = self._child(value.get("__functor"))
            return functor.call(self).auto_call()
        if value.type_name() == "function" and value.is_lambda() and value.has_formals():
            return self._child(value.apply_auto(self._state.state.make_attrs()))
        # A new wrapper, not `self`: each caller releases what it holds.
        return Value(self._state, value)

    def derived_path(self) -> str:
        return f"{self._state.store.get_store_dir()}/{self._forced().drv_path().to_string()}"

    def build(self, build_store: Store | None, build_mode: int, eval_store: Store | None) -> dict[str, Any]:
        value = self._forced()
        drv_path = value.drv_path()
        output_paths = value.output_paths()
        prefix = f"{self._state.store.get_store_dir()}/"
        store = self._state.store if build_store is None else build_store
        target = huggorm_bindings.DerivedPathBuilt(
            drv_path, huggorm_bindings.OutputsSpec(names=sorted(output_paths) or ["out"])
        )
        results = store.store.build_paths_with_results(
            [target], BuildMode(build_mode).huggorm(), None if eval_store is None else eval_store.store
        )
        return {
            "drv_path": prefix + drv_path.to_string(),
            "outputs": {name: prefix + path.to_string() for name, path in output_paths.items() if path is not None},
            "results": [_build_result(result, store.store) for result in results],
        }


class EvalState:
    """A huggorm ``EvalState`` that answers the other engine's method names.

    ``nanopynix._core._objects.CoreEvalState`` is the one caller.
    """

    def __init__(
        self,
        store: Store,
        search_path: list[str] | None = None,
        build_store: Store | None = None,
        eval_settings: dict[str, str] | None = None,
        fetch_settings: dict[str, str] | None = None,
    ) -> None:
        settings = {**(eval_settings or {}), **(fetch_settings or {})}
        if search_path:
            # The other engine puts the search path in front of the `nix-path`
            # setting, as `nix -I` does, so the setting still answers after it.
            configured = huggorm_bindings.get_setting("nix-path") or ""
            settings["nix-path"] = " ".join([*search_path, configured]).strip()
        self.store = store
        self._repl: Any = None
        self.state = huggorm_bindings.EvalState(
            store.store, settings, None if build_store is None else build_store.store
        )
        # `__`, so it is `builtins.sleep` and does not shadow a `sleep` binding.
        self.state.register_primop("__sleep", 1, self._primop("sleep", _sleep))
        for name, (arity, callback) in _primops.items():
            self.state.register_primop(name, arity, self._primop(name, callback))

    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        return _not_ported(f"EvalState.{name}")

    def _primop(self, name: str, callback: Callable[..., Any]) -> Callable[..., Any]:
        # Weak, because the state holds the bridge. A strong reference is a
        # cycle only the cyclic collector frees, and the store stays open until
        # it runs: 20 sessions with one primop held 16 descriptors of the
        # store's database. Nix calls the bridge only inside an evaluation,
        # which a caller reaches through this object, so it is alive then.
        owner = weakref.ref(self)

        def bridge(*arguments: Any) -> Any:
            state = owner()
            if state is None:
                raise RuntimeError("the evaluator that registered this primop is closed")
            converted = [_primop_argument(state.state, name, argument) for argument in arguments]
            try:
                result = callback(*converted)
            except (PrimopError, ValueError) as error:
                # The other engine's rule: these two reject the input, so Nix
                # shows their message bare. huggorm does that for its own errors.
                raise EvalError(str(error)) from error
            return state._make(result)

        return bridge

    def _make_function(self, callback: Callable[..., Any]) -> Any:
        # The other engine's rule: the parameter count is the arity, and a
        # callable with none, or with no signature, is called now.
        try:
            arity = len(inspect.signature(callback).parameters)
        except (TypeError, ValueError):
            arity = 0
        if arity == 0:
            return self._make(callback())
        name = getattr(callback, "__qualname__", type(callback).__qualname__)
        return self.state.make_primop(name, arity, self._primop(name, callback))

    def _make(self, obj: Any) -> Any:
        if isinstance(obj, Value):
            return obj.raw
        if obj is None:
            return self.state.make_null()
        for kind, maker in _SCALAR_MAKERS:
            if isinstance(obj, kind):
                return getattr(self.state, maker)(obj)
        if isinstance(obj, list | tuple):
            made = self.state.make_list()
            for item in cast("list[Any] | tuple[Any, ...]", obj):
                self.state.list_append(made, self._make(item))
            return made
        if isinstance(obj, dict):
            made = self.state.make_attrs()
            for key, item in cast("dict[Any, Any]", obj).items():
                self.state.attrs_set(made, str(key), self._make(item))
            return made
        if callable(obj):
            return self._make_function(obj)
        raise NotPortedError(f"the huggorm engine cannot yet make a Nix value from {type(obj).__name__}")

    def eval_string(self, expression: str, path: str = "<string>") -> Value:
        return Value(self, self.state.eval_expr(expression, path))

    def eval_file(self, path: str) -> Value:
        return Value(self, self.state.eval_file(path))

    def value_from_python(self, obj: object) -> Value:
        return Value(self, self._make(obj))

    # huggorm takes both kinds through one method, as its constructor does.
    def set_eval_setting(self, name: str, value: str) -> None:
        self.state.set_setting(name, value)

    def set_fetch_setting(self, name: str, value: str) -> None:
        self.state.set_setting(name, value)

    def begin_repl(self) -> None:
        if self._repl is not None:
            raise RuntimeError("REPL scope is already active")
        self._repl = self.state.repl()

    def repl_active(self) -> bool:
        return self._repl is not None

    def _scope(self) -> Any:
        if self._repl is None:
            raise RuntimeError("REPL scope is not active")
        return self._repl

    def repl_process_line(self, line: str, path: str) -> Value | None:
        made = self._scope().process_line(line, path)
        return None if made is None else Value(self, made)

    def repl_eval_string(self, expression: str, path: str) -> Value:
        return Value(self, self._scope().eval_expr(expression, path))

    def repl_eval_file(self, path: str) -> Value:
        return Value(self, self._scope().eval_file(path))

    def repl_load_file(self, path: str) -> Value:
        return Value(self, self._scope().load_file(path))

    def repl_add_attrs(self, value: Value) -> list[str]:
        return self._scope().add_attrs(value.raw)

    def repl_scope_names(self) -> list[str]:
        return self._scope().names()

    def repl_select(self, expression: str, path: str = "<string>") -> dict[str, Any] | None:
        selected = self._scope().select(expression, path)
        if selected is None:
            return None
        return {"name": selected.name(), "attrs": Value(self, selected.attrs())}

    def reset_file_cache(self) -> None:
        # `«nix-internal»/derivation-internal.nix` names no file to forget.
        for path in self.state.cached_files():
            if path.startswith("/"):
                self.state.forget_file(path)


def eval_file(state: EvalState, path: str) -> Value:
    return state.eval_file(path)


class LockedFlake:
    """A locked flake, in the shape the other engine's ``LockedFlake`` has."""

    __slots__ = ("raw",)

    def __init__(self, raw: Any) -> None:
        self.raw: Any = raw

    def description(self) -> str:
        return self.raw.description() or ""

    def find_input(self, path: list[str]) -> dict[str, Any] | None:
        node = self.raw.find_input(path)
        if node is None:
            return None
        return {
            "locked_ref": node.locked_ref().to_string(),
            "original_ref": node.original_ref().to_string(),
            "is_flake": node.is_flake(),
        }

    def write_lock_file(self) -> None:
        self.raw.write_lock_file()


def _lock_flake(
    state: EvalState,
    ref: Any,
    update_inputs: bool | list[str] = False,
    write_lock_file: bool = True,
    flake_settings: dict[str, str] | None = None,
) -> LockedFlake:
    """``update_inputs`` is True to recreate the lock file, or the inputs to update."""
    recreate, update = (update_inputs, []) if isinstance(update_inputs, bool) else (False, list(update_inputs))
    return LockedFlake(
        state.state.lock_flake(
            ref, recreate=recreate, update=update, write_lock_file=write_lock_file, settings=flake_settings or {}
        )
    )


def _call_flake(state: EvalState, locked: LockedFlake) -> Value:
    return Value(state, state.state.call_flake(locked.raw))


def _get_flake(state: EvalState, ref: Any) -> Any:
    return state.state.get_flake(ref)


def _metadata_json(state: EvalState, locked: LockedFlake) -> str:
    return state.state.flake_metadata_json(locked.raw)


def _eval_flake(
    state: EvalState, ref: str, write_lock_file: bool = True, flake_settings: dict[str, str] | None = None
) -> Value:
    locked = _lock_flake(state, huggorm_bindings.parse_flake_ref(ref), False, write_lock_file, flake_settings)
    return _call_flake(state, locked)


flake = _not_ported(
    "flake",
    LockedFlake=LockedFlake,
    call_flake=_call_flake,
    eval_flake=_eval_flake,
    get_flake=_get_flake,
    list_flake_settings_metadata_json=huggorm_bindings.flake_settings_json,
    lock_flake=_lock_flake,
    metadata_json=_metadata_json,
    parse_flake_ref=huggorm_bindings.parse_flake_ref,
)
get_flake = _get_flake
lock_flake = _lock_flake
parse_flake_ref = huggorm_bindings.parse_flake_ref


process_connection = _not_ported("process_connection")
register_store_implementation = _not_ported("register_store_implementation")

current_system = huggorm_bindings.current_system
set_setting = huggorm_bindings.set_setting


def build_info() -> dict[str, Any]:
    """The linked Nix's version, and what this engine can do.

    ``boehm_gc`` is a fact about the linked libexpr, and huggorm answers it.
    ``dynamic_primop_registration`` is :func:`register_primop`. The rest are
    nanopynix features the huggorm engine does not offer yet, so each is
    ``False`` until its name is ported. They are here and not absent, because
    callers index them.
    """
    return {
        "nix_version": huggorm_bindings.nix_version(),
        "capabilities": {
            "boehm_gc": huggorm_bindings.boehm_gc(),
            "dynamic_primop_registration": True,
            "eval_statistics": False,
            "store_impl_read_derivation": False,
        },
    }


def enable_experimental_feature(name: str) -> None:
    """Add *name* to the enabled features, and leave the setting's overridden mark.

    An unknown name raises ``RuntimeError``, as the other engine does.
    """
    if not huggorm_bindings.is_experimental_feature(name):
        raise RuntimeError(f"unknown experimental feature: {name}")
    huggorm_bindings.enable_experimental_feature(name)


filter_ansi_escapes = huggorm_bindings.filter_ansi_escapes
get_verbosity = huggorm_bindings.thread_verbosity
set_verbosity = huggorm_bindings.set_thread_verbosity
get_default_verbosity = huggorm_bindings.default_verbosity
set_default_verbosity = huggorm_bindings.set_default_verbosity
get_log_ceiling = huggorm_bindings.process_verbosity
get_logger_request_id = huggorm_bindings.current_request


def set_logger_request_id(request_id: int) -> None:
    """Name the call this thread is inside, for the records it raises.

    ``begin_request`` and not the pair with ``end_request``: the marker
    ``end_request`` pushes says a call ended, and nanopynix pushes its own
    marker after :func:`flush_logs`, so the pump skips huggorm's.
    """
    huggorm_bindings.begin_request(request_id)


#: The interval of huggorm's own server (``LOG_POLL`` in ``huggorm/server.py``).
_LOG_POLL_SECONDS = 0.05
#: The size of ``nanopynix.logging.LogCollector``, one hop further on.
_LOG_QUEUE_CAPACITY = 10_000

_logger = logging.getLogger(__name__)


def _field_values(record: Any) -> list[int | str]:
    return [field.integer() if field.is_int() else field.text() for field in record.fields()]


def _callback_args(record: Any) -> tuple[object, ...] | None:
    """The arguments the other engine's logger passes its callback, after the request id."""
    match record.action():
        case "msg":
            return ("msg", record.level(), record.text())
        case "start":
            return (
                "start",
                record.id(),
                record.level(),
                record.type(),
                record.text(),
                _field_values(record),
                record.parent(),
            )
        case "stop":
            return ("stop", record.id())
        case "result":
            return ("result", record.id(), record.type(), _field_values(record))
        case _:
            return None


@dataclasses.dataclass(slots=True)
class _Tracked:
    type: int
    last_progress: float = 0.0
    last_counts: list[int | str] | None = None


class _ActivityFilter:
    """The other engine's ``ActivityTracker``: what a build monitor reads, and only then.

    Off, a start with text goes up, as ``SimpleLogger`` prints it, and no stop
    or progress does. On, the four types a monitor reads go up whole: start,
    stop, phase, expected and progress. A copy's progress goes up at most once
    per interval; a summary's goes up only when its counts change, because
    Nix sends both summaries on every update of the worker.
    """

    _WANTED = frozenset({ActivityType.COPY_PATH, ActivityType.COPY_PATHS, ActivityType.BUILDS, ActivityType.BUILD})
    _TRACKED_RESULTS = frozenset({ResultType.SET_PHASE, ResultType.PROGRESS, ResultType.SET_EXPECTED})
    _LOG_LINES = frozenset({ResultType.BUILD_LOG_LINE, ResultType.POST_BUILD_LOG_LINE})
    _PROGRESS_INTERVAL = 0.1

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enabled = False
        self._tracked: dict[int, _Tracked] = {}

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = on
            if not on:
                self._tracked.clear()

    def passes(self, record: Any) -> bool:
        match record.action():
            case "start":
                return self._start(record) or bool(record.text())
            case "stop":
                with self._lock:
                    return self._tracked.pop(record.id(), None) is not None
            case "result":
                return record.type() in self._LOG_LINES or self._result(record)
            case _:
                return True

    def _start(self, record: Any) -> bool:
        with self._lock:
            if not self.enabled or record.type() not in self._WANTED:
                return False
            self._tracked[record.id()] = _Tracked(record.type())
            return True

    def _result(self, record: Any) -> bool:
        if record.type() not in self._TRACKED_RESULTS:
            return False
        with self._lock:
            tracked = self._tracked.get(record.id())
            if tracked is None:
                return False
            return record.type() != ResultType.PROGRESS or self._progress_is_news(tracked, record)

    def _progress_is_news(self, tracked: _Tracked, record: Any) -> bool:
        if tracked.type == ActivityType.COPY_PATH:
            now = time.monotonic()
            due = now - tracked.last_progress >= self._PROGRESS_INTERVAL
            if due:
                tracked.last_progress = now
            return due
        counts = [*_field_values(record), 0, 0, 0, 0][:4]
        changed = counts != tracked.last_counts
        tracked.last_counts = counts
        return changed


_activity_filter = _ActivityFilter()


class _LogPump:
    """Drains huggorm's process queue into one callback.

    huggorm queues a record where Nix raises it and never calls Python, so
    something has to read the queue. A thread does, every
    ``_LOG_POLL_SECONDS``, and :meth:`pump` also runs on demand: a call's
    records are in the queue when the call returns, and :func:`flush_logs`
    moves them before the caller marks the call finished.

    The lock orders the two readers. Without it, the thread could hold a
    drained batch while a flush pushes the marker ahead of it.

    A ``fork()`` keeps the lock and not the thread. A child forked mid-drain
    has the lock held for ever, and its first flush hangs. So the parent holds
    the lock across the fork, and the child starts a lock and a thread of its
    own.
    """

    def __init__(self, callback: Callable[..., None]) -> None:
        self.callback = callback
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._dropped = 0
        # At the current default, because subscribing sets the default and
        # installing a logger must not change a level.
        self._stream = huggorm_bindings.subscribe_process_logs(
            capacity=_LOG_QUEUE_CAPACITY, level=huggorm_bindings.default_verbosity()
        )
        self._thread = self._start()

    def _start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="nanopynix-log-pump", daemon=True)
        thread.start()
        return thread

    def before_fork(self) -> None:
        self._lock.acquire()

    def after_fork_in_parent(self) -> None:
        self._lock.release()

    def after_fork_in_child(self) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = self._start()

    def pump(self) -> None:
        with self._lock:
            for record in self._stream.drain():
                if not _activity_filter.passes(record):
                    continue
                args = _callback_args(record)
                if args is None:
                    continue
                try:
                    self.callback(record.request(), *args)
                except Exception:
                    _logger.exception("the log callback failed; the record is lost")
            dropped = self._stream.dropped()
            if dropped != self._dropped:
                _logger.warning("huggorm's log queue discarded %d record(s) so far", dropped)
                self._dropped = dropped

    def _run(self) -> None:
        while not self._stopped.wait(_LOG_POLL_SECONDS):
            self.pump()

    def close(self) -> None:
        self._stopped.set()
        self._thread.join()
        # Before the unsubscribe, which empties the queue.
        self.pump()
        default = huggorm_bindings.default_verbosity()
        huggorm_bindings.unsubscribe_process_logs()
        huggorm_bindings.set_default_verbosity(default)


_log_pump: _LogPump | None = None


def install_logger(callback: Callable[..., None]) -> None:
    """Send every record to *callback*. A second call replaces the first callback."""
    global _log_pump  # noqa: PLW0603 -- one process queue, like the Nix logger it reads
    if _log_pump is None:
        _log_pump = _LogPump(callback)
    else:
        _log_pump.callback = callback


def remove_logger() -> None:
    """Deliver what is queued, and stop reading the queue."""
    global _log_pump
    pump, _log_pump = _log_pump, None
    if pump is not None:
        pump.close()


def _position(pos: Any) -> dict[str, Any] | None:
    if pos is None:
        return None
    return {"file": pos.file(), "line": pos.line(), "column": pos.column()}


def error_detail(exc: BaseException) -> tuple[str, dict[str, Any] | None]:
    """Nix's own rendering of *exc*, and its ``nix::ErrorInfo`` as a dict.

    The dict has the keys ``nix_error_info.hh`` writes. ``pos["file"]`` is
    huggorm's: the file alone, where the other engine appends the line and
    the column to it.
    """
    raw: object = getattr(exc, "colored", "")
    info: Any = getattr(exc, "info", None)
    if not isinstance(info, huggorm_bindings.ErrorInfo):
        return (raw if isinstance(raw, str) else "", None)
    return (
        raw if isinstance(raw, str) else "",
        {
            "level": info.level(),
            "msg": info.msg(),
            "pos": _position(info.pos()),
            "is_from_expr": info.is_from_expr(),
            "status": info.status(),
            "traces": [{"hint": trace.hint(), "pos": _position(trace.pos())} for trace in info.traces()],
            "truncated": info.truncated(),
            "suggestions": list(info.suggestions()),
        },
    )


def _log_test(msg: str) -> None:
    """Log *msg* through Nix's logger at INFO, as the other engine's hook does."""
    huggorm_bindings.log_message(LogLevel.INFO, msg)


def flush_logs() -> None:
    """Deliver every queued record to the callback now."""
    pump = _log_pump
    if pump is not None:
        pump.pump()


# The child keeps the pump that was live at the fork.
_forking_pump: _LogPump | None = None


def _before_fork() -> None:
    global _forking_pump  # noqa: PLW0603 -- one pump per process, like the queue it reads
    _forking_pump = _log_pump
    if _forking_pump is not None:
        _forking_pump.before_fork()


def _after_fork_in_parent() -> None:
    if _forking_pump is not None:
        _forking_pump.after_fork_in_parent()


def _after_fork_in_child() -> None:
    if _forking_pump is not None:
        _forking_pump.after_fork_in_child()


os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_in_parent, after_in_child=_after_fork_in_child)


def set_activity_tracking(on: bool) -> None:
    """Forward what a build monitor reads, as :class:`_ActivityFilter` says."""
    _activity_filter.set_enabled(on)


def get_activity_tracking() -> bool:
    return _activity_filter.enabled


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


list_settings = huggorm_bindings.list_settings


def parse_nix_path(value: str | None = None) -> list[str]:
    """Split a search path as Nix does; ``None`` reads ``NIX_PATH``."""
    raw = os.environ.get("NIX_PATH", "") if value is None else value
    return list(huggorm_bindings.parse_nix_path(raw)) if raw else []


def _enter_evaluator_thread() -> None:
    """Do nothing: huggorm registers a thread with the collector on its first evaluator call."""


expr = _not_ported(
    "expr",
    EvalState=EvalState,
    PrimopError=PrimopError,
    Value=Value,
    _cleanup_primop_registry=_cleanup_primop_registry,
    _enter_evaluator_thread=_enter_evaluator_thread,
    _exit_evaluator_thread=huggorm_bindings.gc_release_thread,
    _gc_collect=huggorm_bindings.collect_garbage,
    _gc_owner_thread_id=huggorm_bindings.collector_owner_thread,
    _gc_stats=huggorm_bindings.gc_stats,
    init_libexpr=init_libexpr,
    eval_file=eval_file,
    is_pseudo_url=is_pseudo_url,
    list_eval_settings_metadata_json=huggorm_bindings.eval_settings_json,
    parse_nix_path=parse_nix_path,
    register_primop=register_primop,
)
store = _not_ported(
    "store",
    BuildMode=BuildMode,
    GCAction=GCAction,
    Store=Store,
    StorePath=huggorm_bindings.StorePath,
    open_store=open_store,
    parse_store_reference=parse_store_reference,
    render_store_reference=huggorm_bindings.render_store_reference,
)


util = _not_ported(
    "util",
    build_info=build_info,
    current_system=current_system,
    enable_experimental_feature=enable_experimental_feature,
    get_activity_tracking=get_activity_tracking,
    get_default_verbosity=get_default_verbosity,
    get_log_ceiling=get_log_ceiling,
    get_logger_request_id=get_logger_request_id,
    _log_test=_log_test,
    get_setting=huggorm_bindings.get_setting,
    get_verbosity=get_verbosity,
    init_libstore=init_libstore,
    install_logger=install_logger,
    list_settings=huggorm_bindings.list_settings,
    list_settings_metadata_json=huggorm_bindings.settings_json,
    remove_logger=remove_logger,
    reset_overridden=huggorm_bindings.reset_overridden,
    set_activity_tracking=set_activity_tracking,
    set_default_verbosity=set_default_verbosity,
    set_logger_request_id=set_logger_request_id,
    set_setting=set_setting,
    set_verbosity=set_verbosity,
)
