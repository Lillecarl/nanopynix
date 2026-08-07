"""One worker process, whichever transport started it.

``WorkerClient`` needs five things from the process that runs Nix: its pid, its
exit status, whether it is still going, a bounded wait, and the two signals
that end it. A ``multiprocessing.Process`` and an
``asyncio.subprocess.Process`` both offer all five, under different names and
with a different blocking model, so this module states the five and adapts
each one.

**The exit status is why this is not merely tidy.** An abort, a segmentation
fault and an ordinary exit are one closed pipe at the transport, and the status
is the only thing that tells them apart -- see ``WorkerClient``'s exit-status
block and issue #55. Both process classes report a signal as a negative
number, because both pass the wait status through
``os.waitstatus_to_exitcode``, so :class:`~nanopynix.WorkerSignaledError` reads
one convention on both start methods.

``join`` is asynchronous here, and it is the one member whose shape had to
change. ``multiprocessing.Process.join`` blocks, so its adapter goes through
``anyio.to_thread``; ``asyncio.subprocess.Process.wait`` is a coroutine on the
loop that owns the child, and handing it to a thread would take it away from
the watcher that resolves it.
"""

from __future__ import annotations

import contextlib
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anyio
import anyio.to_thread

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    import asyncio


@runtime_checkable
class WorkerProcess(Protocol):
    """What the client needs from the process running Nix.

    ``@runtime_checkable``, so that beartype can build an ``isinstance`` test
    from it. A plain ``Protocol`` cannot be checked, and beartype answers by
    leaving the whole annotated function undecorated rather than by failing --
    see the account of ``IServable`` in ``rpc/worker/_worker.py``, which is the
    same mistake made once already.
    """

    __slots__ = ()  # in the body, and load-bearing -- see nanopynix.protocols

    @property
    @abstractmethod
    def pid(self) -> int | None:
        """The process identifier, or ``None`` before it exists."""

    @property
    @abstractmethod
    def exit_status(self) -> int | None:
        """How the process ended, without waiting for it.

        ``None`` while it runs, and also while the parent has not reaped it
        yet. Negative for a signal.
        """

    @abstractmethod
    def is_alive(self) -> bool:
        """Whether the process is still running."""

    @abstractmethod
    async def join(self, timeout: float) -> None:  # noqa: ASYNC109 -- see the class docstring: this must not raise on expiry
        """Wait up to *timeout* seconds for the process to end.

        **It returns either way, and that is why it is not ``fail_after``.**
        The caller reads :attr:`exit_status` afterwards to find out which
        happened, and every caller is already carrying an exception out of a
        scope that may be cancelled. A ``TimeoutError`` raised here would
        replace the one the caller is reporting -- see
        ``WorkerClient._reap_worker``.
        """

    @abstractmethod
    def terminate(self) -> None:
        """Send SIGTERM. A process that has already ended is not an error."""

    @abstractmethod
    def kill(self) -> None:
        """Send SIGKILL. A process that has already ended is not an error."""


class MultiprocessingWorkerProcess(WorkerProcess):
    """A worker the forkserver or ``spawn`` started.

    ``Any`` for the process, because ``multiprocessing`` builds it from a
    context and the class differs per start method.
    """

    __slots__ = ("_proc",)

    def __init__(self, proc: Any) -> None:
        self._proc: Any = proc

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    @property
    def exit_status(self) -> int | None:
        return self._proc.exitcode

    def is_alive(self) -> bool:
        return bool(self._proc.is_alive())

    async def join(self, timeout: float) -> None:  # noqa: ASYNC109 -- returns on expiry rather than raising; see WorkerProcess.join
        # `anyio.to_thread.run_sync`, and not `asyncio.to_thread`: a pool
        # shutdown stops every worker at once, and asyncio spawns one
        # unbounded thread per call. anyio's shared limiter puts a ceiling on
        # that. `multiprocessing._stop_process` says the same thing.
        await anyio.to_thread.run_sync(self._proc.join, timeout)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()


class StdioWorkerProcess(WorkerProcess):
    """A worker started by ``exec``, over its stdin and stdout."""

    __slots__ = ("_proc",)

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc: asyncio.subprocess.Process = proc

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    @property
    def exit_status(self) -> int | None:
        return self._proc.returncode

    def is_alive(self) -> bool:
        return self._proc.returncode is None

    async def join(self, timeout: float) -> None:  # noqa: ASYNC109 -- returns on expiry rather than raising; see WorkerProcess.join
        # `move_on_after`, which is `fail_after` without the raise. That is
        # the whole difference the rule above is about.
        with anyio.move_on_after(timeout):
            await self._proc.wait()

    def terminate(self) -> None:
        # ProcessLookupError: asyncio refuses a signal to a process it has
        # already reaped, and every caller here has just decided the process
        # was alive. The two moments are not the same moment.
        with contextlib.suppress(ProcessLookupError):
            self._proc.terminate()

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self._proc.kill()
