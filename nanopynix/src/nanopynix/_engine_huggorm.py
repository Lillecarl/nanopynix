"""The engine: nanopynix's engine surface, answered by huggorm's bindings.

An adapter. It gives huggorm's names the shapes nanopynix's callers were
written against, and its callers move onto huggorm's own API one area at a
time until it is empty. ``nanopynix._engine`` is the only module that imports
it.
"""

from __future__ import annotations

import dataclasses
import importlib.resources
import itertools
import logging
import os
import threading
import time
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import huggorm_bindings  # type: ignore[reportMissingImports] -- installed only in the huggorm scope
from huggorm_bindings import errors as huggorm_errors  # type: ignore[reportMissingImports] -- as above
from huggorm_bindings.errors import BadStorePath, EvalError, NixError  # type: ignore[reportMissingImports] -- as above
from nanopynix_proto.nix.common import ActivityType, LogLevel, ResultType

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


STORE_DISPATCH_METHODS: tuple[str, ...] = ()


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
        case "msg" if (info := record.info()) is not None:
            return ("error", record.level(), info.msg(), _info_dict(info))
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
    return (raw if isinstance(raw, str) else "", _info_dict(info))


def _info_dict(info: Any) -> dict[str, Any]:
    """*info* with the keys ``nix_error_info.hh`` writes."""
    return {
        "level": info.level(),
        "msg": info.msg(),
        "pos": _position(info.pos()),
        "is_from_expr": info.is_from_expr(),
        "status": info.status(),
        "traces": [{"hint": trace.hint(), "pos": _position(trace.pos())} for trace in info.traces()],
        "truncated": info.truncated(),
        "suggestions": list(info.suggestions()),
    }


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
