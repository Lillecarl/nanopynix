"""A session held across ``fork()`` refuses to work, on both engines.

Issue #64. ``ansible-core`` pins ``multiprocessing.get_context('fork')`` and
forks once for each (host, task) pair, so a session that is open when a plugin
returns reaches every child. Neither engine noticed, and each failed somewhere
other than the fork.

**Each test here forks the pytest process, which carries threads.** That is the
hazard under test and not an accident. The child only reads a flag and raises,
so it needs no lock that a thread lost to the fork still holds. Every child is
bounded twice: the operation runs on a thread of the child's own with a
deadline, and the parent waits for the report with another.

What the guard prevents, measured on a ``ThreadPoolExecutor`` with one warm
thread that such a child inherits::

    parent warm-up:  3888226
    child submit:    TimeoutError after 3 s
    child threads:   ['nix-store_0']   alive: [False]

``NixThreadExecutor`` awaits that future with no deadline, so the unguarded
call hangs until the process ends. ``_outcome_of`` reports ``hung`` for it,
rather than taking the run down with it.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import threading
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import pytest

from nanopynix._fork import ForkGuard
from nanopynix._wire import HandleKind
from nanopynix.exceptions import ForkedSessionError
from nanopynix.rpc.client._session import _DeferredReleases

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from multiprocessing.connection import Connection

    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory

#: How long the child gives one operation before it calls the operation hung.
_OPERATION_DEADLINE_SECONDS = 10.0

#: How long the parent waits for the child's report, and then for the child.
_CHILD_DEADLINE_SECONDS = 30.0

Outcome = tuple[str, str]

pytestmark = pytest.mark.anyio


def _outcome_of(operation: Callable[[], Coroutine[Any, Any, object]]) -> Outcome:
    """Run one coroutine in this child, and name what happened to it.

    **On a thread of this child's own, and this is not a style choice.** A fork
    inherits the running-loop state of its parent, so ``asyncio.run`` on the
    inherited thread raises "cannot be called from a running event loop" about
    a loop that did not survive the fork. A new thread has no running loop, and
    that answer needs no private asyncio API.

    ``hung`` is the outcome that matters. Without the guard the operation waits
    for a thread that the fork did not keep, and the deadline here is what
    turns that wait into a report.
    """
    outcome: list[Outcome] = []

    def target() -> None:
        try:
            asyncio.run(operation())
        except BaseException as exc:
            outcome.append((type(exc).__name__, str(exc)))
        else:
            outcome.append(("ok", ""))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(_OPERATION_DEADLINE_SECONDS)
    if not outcome:
        return ("hung", f"no answer within {_OPERATION_DEADLINE_SECONDS}s")
    return outcome[0]


def _child_reports(sender: Connection, operation: Callable[[], Coroutine[Any, Any, object]]) -> None:
    """The body of the forked child. ``multiprocessing`` calls ``os._exit`` after it."""
    sender.send(_outcome_of(operation))
    sender.close()


def _ask_a_fork(operation: Callable[[], Coroutine[Any, Any, object]]) -> Outcome:
    """Fork, run *operation* in the child, and return what the child reported.

    ``get_context("fork")`` rather than the default start method, because that
    is what Ansible pins and therefore what this issue is about. The child
    inherits *operation* through memory, so the closure needs no pickling.
    """
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_reports, args=(sender, operation))
    process.start()
    sender.close()
    try:
        if not receiver.poll(_CHILD_DEADLINE_SECONDS):
            return ("no report", f"the child said nothing within {_CHILD_DEADLINE_SECONDS}s")
        return receiver.recv()
    finally:
        receiver.close()
        process.join(_CHILD_DEADLINE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()


async def _in_a_fork(operation: Callable[[], Coroutine[Any, Any, object]]) -> Outcome:
    """``_ask_a_fork``, off the event loop of this test."""
    return await anyio.to_thread.run_sync(_ask_a_fork, operation)


# ════════════════════════════════════════════════════════════════════
# The two mechanisms, without a fork
# ════════════════════════════════════════════════════════════════════


def test_a_guard_answers_for_the_process_that_built_it() -> None:
    """A pid that is not this one is a fork, whether or not the hook saw it.

    This is the half that catches a raw ``fork(2)`` through ``ctypes``, which
    ``os.register_at_fork`` does not see at all.
    """
    guard = ForkGuard("a test subject")
    assert not guard.forked
    guard.check()

    # Standing in for the fork this test does not perform.
    guard._created_pid = os.getpid() + 1
    assert guard.forked
    with pytest.raises(ForkedSessionError, match="cannot cross a fork"):
        guard.check()


async def test_a_subprocess_is_not_a_fork(rpc_session: RpcSessionFactory) -> None:
    """Spawning a process must not mark a session forked.

    ``anyio.open_process`` is how this repository runs every static gate and
    every ``nix`` command, so a false positive here would stop the whole suite
    rather than one test. Measured: ``subprocess.run`` leaves the
    ``after_in_child`` list of the parent empty.
    """
    async with rpc_session() as nix:
        process = await anyio.open_process(["/bin/sh", "-c", "exit 0"])
        await process.wait()
        assert await nix.get_verbosity() is not None


# ════════════════════════════════════════════════════════════════════
# inproc
# ════════════════════════════════════════════════════════════════════


async def test_an_inproc_operation_in_a_fork_is_refused(inproc_session: InprocSessionFactory) -> None:
    """The child gets an exception, and not the deadlock the pool would give it."""
    async with inproc_session() as nix:
        name, message = await _in_a_fork(nix.get_verbosity)
        assert name == "ForkedSessionError", message
        assert "cannot cross a fork" in message


async def test_an_inproc_fork_tears_nothing_down(inproc_session: InprocSessionFactory) -> None:
    """A child that closes the session leaves the parent's session working.

    The executor's threads, the evaluators, the stores and the process guard
    all belong to the parent. Closing them from the child would unregister
    Boehm threads that are not this process's, and it cannot even finish: the
    executor has no thread here to run the teardown on.
    """
    async with inproc_session() as nix:
        assert await _in_a_fork(nix.close) == ("ok", "")
        assert await nix.get_verbosity() is not None


async def test_a_fork_cannot_open_an_inproc_session_of_its_own(inproc_session: InprocSessionFactory) -> None:
    """A child whose parent holds a session is refused a session of its own.

    Issue #64 asks for the process guard to be cleared here. It is not: the
    child inherits an initialised libexpr and a Boehm thread table that lists
    threads that no longer exist. The supported pattern is untouched by this,
    because a parent that never opened a session leaves the guard empty --
    fork first, then open.
    """
    async with inproc_session() as nix:
        assert await nix.get_verbosity() is not None

        async def open_another() -> None:
            async with inproc_session():
                pass

        name, message = await _in_a_fork(open_another)
        assert name == "ForkedSessionError", message
        assert "inherited an open" in message


# ════════════════════════════════════════════════════════════════════
# rpc
# ════════════════════════════════════════════════════════════════════


async def test_an_rpc_operation_in_a_fork_is_refused(rpc_session: RpcSessionFactory) -> None:
    """The child must not write to a worker that answers the parent as well."""
    async with rpc_session() as nix:
        name, message = await _in_a_fork(nix.get_verbosity)
        assert name == "ForkedSessionError", message
        assert "cannot cross a fork" in message


async def test_an_rpc_fork_leaves_the_workers_of_its_parent_alone(rpc_session: RpcSessionFactory) -> None:
    """A child that closes the session must not stop the parent's worker.

    ``Process.terminate()`` asks no question about which process is calling.
    ``join`` and ``is_alive`` do assert it, so a stock interpreter turns this
    into ``AssertionError: can only test a child process`` from inside a
    shielded teardown -- and ``python -O`` removes both asserts, after which
    the SIGTERM does arrive.

    The RPC below proves the worker still serves this process. The ``async
    with`` that follows proves nothing signalled it: ``Session.close`` raises
    ``WorkerSignaledError`` for a worker that died of a signal this process did
    not send.
    """
    async with rpc_session() as nix:
        assert await _in_a_fork(nix.close) == ("ok", "")
        assert await nix.get_verbosity() is not None


def test_a_forked_evaluator_queues_and_hands_back_no_release() -> None:
    """A lease that a fork collects must never reach the worker of the parent.

    ``weakref.finalize`` has no process check of its own, unlike
    ``multiprocessing.util.Finalize``, so the lease finalizer of every value
    still runs in a forked child. It only queues, and this queue is what the
    guard empties: a release sent from there would free a handle the parent
    still uses.

    The pid stands in for the fork, as in
    :func:`test_a_guard_answers_for_the_process_that_built_it`. A real fork
    would prove the same line and would need a live worker to do it.
    """
    releases = _DeferredReleases()
    lease = releases.new_lease(HandleKind.VALUE, 7)
    ref = lease.claim()
    assert ref is not None

    # Standing in for the fork this test does not perform.
    releases._fork._created_pid = os.getpid() + 1
    releases.defer(ref)
    assert releases.drain() == []
