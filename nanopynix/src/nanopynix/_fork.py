"""One at-fork hook for the whole process, and the flag it sets.

**Nothing this library owns survives a ``fork()``.** An rpc ``Session`` holds a
pipe to one serial worker, and the child inherits the same pipe: two processes
then write to one worker, and the protocol desynchronises. An inproc
``Session`` holds a thread pool, and ``fork()`` keeps only the calling thread:
the child submits work to a pool whose threads no longer exist and waits for a
future that nothing will ever complete.

Neither engine noticed. Each failed somewhere other than the fork, which is why
this module exists: a forked object refuses at once, and names the fork.

Measured, on a ``ThreadPoolExecutor`` with one warm thread that a
``multiprocessing`` ``fork`` child inherits::

    parent warm-up:  3888226
    child submit:    TimeoutError after 3 s
    child threads:   ['nix-store_0']   alive: [False]

``NixThreadExecutor`` awaits that future with no deadline, so the same call in
a child hangs until the process ends.

**Two mechanisms, because each one misses what the other catches.**
``os.register_at_fork`` sees ``os.fork()`` and every ``multiprocessing`` start
method that forks. It does not see a raw ``fork(2)`` through ``ctypes`` or a C
extension that forks itself. The comparison of :func:`os.getpid` against the
pid that built the guard sees those. Its own blind spot is a pid namespace,
where a child can be given the pid of its parent, and
:class:`~nanopynix.OverlayNamespace` makes that a real configuration here.

**The handler sets flags, and does nothing else.** It runs in the child with
one surviving thread. A lock that another thread held at the moment of the fork
stays held for ever, so the handler takes no lock, writes no log and touches no
file.

**One handler serves the process.** ``os.unregister_at_fork`` does not exist,
so an object that registered its own would leave that handler behind for the
life of the process. Every guard goes in the weak registry below instead.
"""

from __future__ import annotations

import os
import weakref

from nanopynix.exceptions import ForkedSessionError

# Every live guard in this process. Weak, because a guard must not keep the
# session that owns it alive.
_REGISTRY: weakref.WeakSet[ForkGuard] = weakref.WeakSet()

# The same two mechanisms as `ForkGuard`, at the scope of the process rather
# than of one object. A guard cannot answer this: it is built when a session
# is, and the question "is this process a fork" has to be answerable before
# anything is built.
_IMPORT_PID = os.getpid()
_process_forked = False


def process_is_forked() -> bool:
    """Whether this process is a fork of the one that imported this module.

    **What reads it.** ``NanopynixSettings.worker_start`` resolves ``"auto"``
    with this. A forkserver worker cannot be started from a forked child at
    all: ``multiprocessing.forkserver.ForkServer`` carries no pid guard, so
    ``ensure_running`` calls ``os.waitpid(self._forkserver_pid, WNOHANG)`` on a
    process that is not this one's child and raises ``ChildProcessError``.
    ``spawn`` has no such singleton, and is measured to work there.
    """
    return _process_forked or os.getpid() != _IMPORT_PID


class ForkGuard:
    """Whether the object that holds this guard has crossed a ``fork()``.

    Held by composition rather than inherited. A mixin would give every
    ``__slots__`` class that uses it a ``__dict__``, and it would put this
    module in the MRO of three unrelated classes for one boolean.
    """

    __slots__ = ("__weakref__", "_created_pid", "_forked", "_subject")

    def __init__(self, subject: str) -> None:
        #: What to call the owner in the error message, for example "rpc Session".
        self._subject = subject
        self._created_pid = os.getpid()
        self._forked = False
        _REGISTRY.add(self)

    @property
    def forked(self) -> bool:
        """Whether this process is a fork of the one that built the owner."""
        return self._forked or os.getpid() != self._created_pid

    def check(self) -> None:
        """Raise :class:`ForkedSessionError` when this process is a fork."""
        if self.forked:
            raise ForkedSessionError(
                f"{self._subject} belongs to process {self._created_pid}, and this is process {os.getpid()}. "
                "A session cannot cross a fork(): open one in this process instead."
            )


def _mark_the_child_forked() -> None:
    """Set the process flag, and the flag on every guard. Runs in the child.

    Takes no lock, and ``list`` first, so that a weak reference which dies
    during the walk cannot change the set under the iterator.
    """
    global _process_forked  # noqa: PLW0603 -- one process-wide fact, set once by the one at-fork hook
    _process_forked = True
    for guard in list(_REGISTRY):
        guard._forked = True  # type: ignore[reportPrivateUsage] -- this module owns the flag  # noqa: SLF001


os.register_at_fork(after_in_child=_mark_the_child_forked)
