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
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, NoReturn

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
fetchers = _not_ported("fetchers")
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
        self._thread = threading.Thread(target=self._run, name="nanopynix-log-pump", daemon=True)
        self._thread.start()

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


expr = _not_ported("expr", is_pseudo_url=is_pseudo_url, parse_nix_path=parse_nix_path)
store = _not_ported("store", render_store_reference=huggorm_bindings.render_store_reference)


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
