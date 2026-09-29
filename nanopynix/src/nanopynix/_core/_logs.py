"""Nix's log records, delivered to one Python callback.

huggorm queues a record where Nix raises it and never calls Python. This
module reads that queue and hands each record to the callback that
``install_logger`` names, as ``callback(request_id, action, *args)``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from nanopynix_proto.nix.common import ActivityType, ResultType

from nanopynix._engine import (
    begin_request,
    default_verbosity,
    set_default_verbosity,
    subscribe_process_logs,
    unsubscribe_process_logs,
)
from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import error_info_dict

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable

    from nanopynix._engine import LogRecord

#: The interval of huggorm's own server (``LOG_POLL`` in ``huggorm/server.py``).
_LOG_POLL_SECONDS = 0.05
#: The size of ``nanopynix.logging.LogCollector``, one hop further on.
_LOG_QUEUE_CAPACITY = 10_000

_logger = logging.getLogger(__name__)


def set_logger_request_id(request_id: int) -> None:
    """Name the call this thread is inside, for the records it raises.

    ``begin_request`` and not the pair with ``end_request``: the marker
    ``end_request`` pushes says a call ended, and nanopynix pushes its own
    marker after :func:`flush_logs`, so the pump skips huggorm's.
    """
    begin_request(request_id)


def _field_values(record: LogRecord) -> list[int | str]:
    return [field.integer() if field.is_int() else field.text() for field in record.fields()]


def _callback_args(record: LogRecord) -> tuple[object, ...] | None:
    """The arguments the callback takes after the request id."""
    match record.action():
        case "msg" if (info := record.info()) is not None:
            return ("error", record.level(), info.msg(), error_info_dict(info))
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
    """What a build monitor reads, and only while one asks.

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

    def passes(self, record: LogRecord) -> bool:
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

    def _start(self, record: LogRecord) -> bool:
        with self._lock:
            if not self.enabled or record.type() not in self._WANTED:
                return False
            self._tracked[record.id()] = _Tracked(record.type())
            return True

    def _result(self, record: LogRecord) -> bool:
        if record.type() not in self._TRACKED_RESULTS:
            return False
        with self._lock:
            tracked = self._tracked.get(record.id())
            if tracked is None:
                return False
            return record.type() != ResultType.PROGRESS or self._progress_is_news(tracked, record)

    def _progress_is_news(self, tracked: _Tracked, record: LogRecord) -> bool:
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

    A thread reads the queue every ``_LOG_POLL_SECONDS``, and :meth:`pump`
    also runs on demand: a call's records are in the queue when the call
    returns, and :func:`flush_logs` moves them before the caller marks the
    call finished.

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
        self._stream = subscribe_process_logs(capacity=_LOG_QUEUE_CAPACITY, level=default_verbosity())
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
        default = default_verbosity()
        unsubscribe_process_logs()
        set_default_verbosity(default)


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


def set_activity_tracking(on: bool) -> None:
    """Forward what a build monitor reads, as :class:`_ActivityFilter` says."""
    _activity_filter.set_enabled(on)


def get_activity_tracking() -> bool:
    return _activity_filter.enabled


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
