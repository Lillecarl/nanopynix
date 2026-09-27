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

import itertools
import json
import logging
import os
import threading
import weakref
from typing import TYPE_CHECKING, Any, NoReturn, cast

import huggorm_bindings  # type: ignore[reportMissingImports] -- installed only in the huggorm scope

from nanopynix._typechecking import BEARTYPING

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

errors = _not_ported("errors")
flake = _not_ported("flake")
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

get_env_sh_path = _not_ported("get_env_sh_path")

eval_counters_enabled = _not_ported("eval_counters_enabled")


def init_libexpr() -> None:
    """Enable ``fetch-tree``, as the other engine's ``init_libexpr`` does.

    That function also starts the Boehm collector. huggorm starts it at
    import, so the feature is all that is left.
    """
    enable_experimental_feature("fetch-tree")


is_pseudo_url = huggorm_bindings.is_pseudo_url
set_eval_counters_enabled = _not_ported("set_eval_counters_enabled")

input_from_attrs = _not_ported("input_from_attrs")
input_from_url = _not_ported("input_from_url")

get_flake = _not_ported("get_flake")
lock_flake = _not_ported("lock_flake")
parse_flake_ref = _not_ported("parse_flake_ref")

STORE_DISPATCH_METHODS: tuple[str, ...] = ()
BuildMode = _not_ported("BuildMode")


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

    def follow_links_to_store_path(self, path: str) -> Any:
        return self.store.follow_links_to_store_path(path)

    def query_path_from_hash_part(self, hash_part: str) -> Any:
        return self.store.query_path_from_hash_part(hash_part)

    def query_all_valid_paths(self) -> list[Any]:
        return self.store.query_all_valid_paths()

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

    def as_int(self) -> int:
        return self._forced().integer()

    def as_float(self) -> float:
        return self._forced().floating()

    def as_bool(self) -> bool:
        return self._forced().boolean()

    def as_string(self) -> str:
        return self._forced().string_value()

    def realise_string(self) -> str:
        return self._forced().realise_string()

    def realise_argv(self) -> list[str]:
        return self._forced().realise_argv()

    def attr_get(self, name: str) -> Value:
        return self._child(self._forced().get(name))

    def has_attr(self, name: str) -> bool:
        return self._forced().has(name)

    def attr_names(self) -> list[str]:
        value = self._forced()
        return [value.name_at(index) for index in range(value.size())]

    def list_get(self, index: int) -> Value:
        return self._child(self._forced().at(index))

    def list_length(self) -> int:
        return self._forced().size()

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
        return self

    def derived_path(self) -> str:
        return f"{self._state.store.get_store_dir()}/{self._forced().drv_path().to_string()}"


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
        self.state = huggorm_bindings.EvalState(
            store.store, settings, None if build_store is None else build_store.store
        )
        for name, (arity, callback) in _primops.items():
            self.state.register_primop(name, arity, self._primop(callback))

    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        return _not_ported(f"EvalState.{name}")

    def _primop(self, callback: Callable[..., Any]) -> Callable[..., Any]:
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
            converted = [json.loads(argument.to_json(False)) for argument in arguments]
            return state._make(callback(*converted))

        return bridge

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
        raise NotPortedError(f"the huggorm engine cannot yet make a Nix value from {type(obj).__name__}")

    def eval_string(self, expression: str, path: str = "<string>") -> Value:
        return Value(self, self.state.eval_expr(expression, path))

    def eval_file(self, path: str) -> Value:
        return Value(self, self.state.eval_file(path))

    def value_from_python(self, obj: object) -> Value:
        return Value(self, self._make(obj))

    def repl_active(self) -> bool:
        """No: this engine has no REPL scope yet, so none is ever active."""
        return False

    def reset_file_cache(self) -> None:
        # `«nix-internal»/derivation-internal.nix` names no file to forget.
        for path in self.state.cached_files():
            if path.startswith("/"):
                self.state.forget_file(path)


def eval_file(state: EvalState, path: str) -> Value:
    return state.eval_file(path)


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
    """Add *name* to the enabled features, as ``extra-experimental-features`` does in nix.conf."""
    huggorm_bindings.set_setting("extra-experimental-features", name)


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


_activity_tracking = False


def set_activity_tracking(on: bool) -> None:
    """Record the choice. huggorm has no activity filter yet, so it sends every activity.

    Every activity is a superset of what tracking forwards, so a build
    monitor still sees its builds and copies. The filter that narrows the
    rest is the next step of huggorm ``tasks/097``.
    """
    global _activity_tracking  # noqa: PLW0603 -- process-wide, like the filter it stands for
    _activity_tracking = on


def get_activity_tracking() -> bool:
    return _activity_tracking


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
    init_libexpr=init_libexpr,
    eval_file=eval_file,
    is_pseudo_url=is_pseudo_url,
    parse_nix_path=parse_nix_path,
    register_primop=register_primop,
)
store = _not_ported(
    "store",
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
