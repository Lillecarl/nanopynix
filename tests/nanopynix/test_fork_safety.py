"""What a ``fork()`` does to a session, and what a forked child may open next.

Issues #64 and #100. ``ansible-core`` pins
``multiprocessing.get_context('fork')`` and forks once for each (host, task)
pair, so a session that is open when a plugin returns reaches every child.
Neither engine noticed, and each failed somewhere other than the fork.

Two questions, and this file answers both:

1. **May a child use the session it inherited?** No, on either engine. That is
   #64, and the first two thirds of this file.
2. **May a child open one of its own?** inproc, no -- for the life of the
   process, once Nix is initialised in that address space. rpc, yes: Nix lives
   in a worker process, so the child starts one of its own. That is #100, and
   the last third.

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
import sys
import threading
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import pytest

from nanopynix._fork import ForkGuard
from nanopynix._wire import HandleKind
from nanopynix.exceptions import ForkedSessionError
from nanopynix.rpc.client._session import _DeferredReleases
from nanopynix.settings import NanopynixSettings, resolve_worker_start
from tests.support.subprocess_output import run_process

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from multiprocessing.connection import Connection

    from tests.support.nix_environment import InprocSessionFactory, NixTestEnvironment, RpcSessionFactory

#: How long the child gives one operation before it calls the operation hung.
_OPERATION_DEADLINE_SECONDS = 10.0

#: How long the parent waits for the child's report, and then for the child.
_CHILD_DEADLINE_SECONDS = 30.0

Outcome = tuple[str, str]

# `forks_the_process` keeps every test here out of the concurrency soak. A lane
# runs beside seven others, and a fork of a process in the middle of that work
# keeps only the calling thread. The soak also lends one Session to every lane,
# so the test below that needs a *second* Session cannot have one. See
# `_DISQUALIFYING_MARKS` in tests/support/soak.py for the measurement.
pytestmark = [pytest.mark.anyio, pytest.mark.forks_the_process]


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


# ════════════════════════════════════════════════════════════════════
# What a process may do after a fork, per engine
# ════════════════════════════════════════════════════════════════════

#: A whole program, for a process that has never initialised anything.
#:
#: This cannot run in the pytest process, and that is the point. Something
#: there has always called ``init_libexpr`` by the time a test runs, which is
#: exactly the state that hid issue #54.
_EVALUATOR_WITH_NO_INIT_LIBEXPR = """
import sys

import nanopynix

nanopynix.init_libstore(load_config=False)
store = nanopynix.open_store(sys.argv[1])
nanopynix.EvalState(store, [])
print("constructed")
"""

#: A whole program: fork a process that has never touched Nix, and open an
#: inproc session in the child. The supported pattern, and the one ansinix
#: relies on.
_FORK_FIRST_THEN_OPEN = """
import asyncio
import os
import sys

import nanopynix
import nanopynix.inproc


async def use_a_session():
    session = nanopynix.inproc.Session(
        store_uri=sys.argv[1],
        load_config=False,
        settings=nanopynix.NixSettings.model_validate_json(sys.argv[2]),
    )
    async with session as nix:
        # A round trip, so the child proves it reached Nix and not only a
        # constructor. Anything that raises exits this program nonzero.
        await nix.get_verbosity()


# The parent opens nothing, so the process guard is empty when it forks.
if os.fork() == 0:
    asyncio.run(use_a_session())
    print("child opened a session")
    # `os._exit` flushes nothing, and stdout is a pipe here, so it is block
    # buffered and the line above would never leave the process.
    sys.stdout.flush()
    os._exit(0)
_, status = os.waitpid(0, 0)
# A wait status is not an exit code: a child killed by a signal encodes the
# signal in the low bits, and `sys.exit` of the raw value would report success.
sys.exit(os.waitstatus_to_exitcode(status))
"""


async def test_an_evaluator_needs_no_init_libexpr_before_it(shared_nix_environment: NixTestEnvironment) -> None:
    """``nanopynix.EvalState(store)`` constructs, rather than aborting the process.

    **Issue #54.** ``PyEvalState::init`` registered the calling thread with
    Boehm and never started the collector, so the process died on SIGABRT with
    nothing a caller could catch. Measured, by taking the collector start back
    out::

        exited -6
        --- stderr ---
        Threads explicit registering is not previously enabled

    bdwgc aborts first, because ``GC_allow_register_threads`` runs inside
    ``GC_INIT``. Behind it waits Nix's own ``assertGCInitialized()``, a bare
    ``assert`` in the ``EvalState`` constructor. One call answers both.

    The fork it was reported in was incidental: a forked child is simply a
    process where nothing had called ``init_libexpr`` yet.

    A subprocess, and not a fork, because the pytest process has initialised
    already and cannot un-initialise.
    """
    result = await run_process(
        [sys.executable, "-c", _EVALUATOR_WITH_NO_INIT_LIBEXPR, shared_nix_environment.store_uri]
    )
    assert result.returncode == 0, result.describe()
    assert "constructed" in result.stdout


async def test_a_fork_after_a_closed_inproc_session_is_still_refused(
    inproc_session: InprocSessionFactory,
) -> None:
    """Closing the session gives the collector back to nobody.

    ``release`` clears the active session, so the guard looks empty after a
    close. Nix initialisation does not come back out: ``init_libexpr`` parks a
    ``nix-gc-owner`` thread that owns Boehm's one static ``first_thread``
    entry and never exits, and ``fork()`` keeps only the calling thread. A
    child that opened a "fresh" session over that entry would collect against
    a thread that does not exist, which is issues #53, #69 and #72.

    This is the hole that #64 left. It passed before this change, because
    nothing refused the child at all.
    """
    async with inproc_session() as nix:
        assert await nix.get_verbosity() is not None

    async def open_another() -> None:
        async with inproc_session():
            pass

    name, message = await _in_a_fork(open_another)
    assert name == "ForkedSessionError", message
    assert "initialized Nix in this address space" in message


async def test_fork_first_then_open_still_works_for_inproc(shared_nix_environment: NixTestEnvironment) -> None:
    """The one supported inproc pattern, and the test that stops the guard eating it.

    A parent that never opened a session leaves the process guard empty, so a
    child of it is an ordinary process as far as Nix is concerned. The refusal
    above must not reach this.

    A subprocess forks here, because the pytest process has itself initialised
    Nix and is therefore the case that gets refused.
    """
    result = await run_process(
        [
            sys.executable,
            "-c",
            _FORK_FIRST_THEN_OPEN,
            shared_nix_environment.store_uri,
            shared_nix_environment.settings.model_dump_json(),
        ]
    )
    assert result.returncode == 0, result.describe()
    assert "child opened a session" in result.stdout


async def test_a_forked_child_opens_an_rpc_session_of_its_own(rpc_session: RpcSessionFactory) -> None:
    """The rpc half of the contract: refuse the inherited one, allow a new one.

    Nix runs in a worker process, so a child of the client inherits no Nix at
    all. What it does inherit is the ``multiprocessing`` forkserver, which
    carries no pid guard: ``ensure_running`` calls
    ``os.waitpid(self._forkserver_pid, WNOHANG)`` on a process that is not its
    child and raises ``ChildProcessError``. ``worker_start="auto"`` answers
    ``spawn`` here, which has no such singleton.
    """
    async with rpc_session() as nix:
        assert await nix.get_verbosity() is not None

        async def open_another() -> None:
            async with rpc_session() as child_nix:
                # A round trip, and not only a constructor: this is what proves
                # the child spoke to a worker of its own. Anything that raises
                # here reaches the assertion below as its own exception name.
                await child_nix.get_verbosity()

        assert await _in_a_fork(open_another) == ("ok", "")

        # And the parent's own worker is untouched by any of it.
        assert await nix.get_verbosity() is not None


async def test_an_rpc_session_starts_a_spawn_worker_with_no_fork(rpc_session: RpcSessionFactory) -> None:
    """``spawn`` is covered on its own, and not only inside a forked child.

    A failure that only ever appeared under ``worker_start="auto"`` in a fork
    would have two candidate causes. This leaves one.
    """
    async with rpc_session(runtime_settings=NanopynixSettings(worker_start="spawn")) as nix:
        assert await nix.get_verbosity() is not None


@pytest.mark.parametrize(
    ("forked", "expected"),
    [(False, "forkserver"), (True, "spawn")],
)
def test_auto_picks_the_start_method_from_the_fork(
    monkeypatch: pytest.MonkeyPatch, forked: bool, expected: str
) -> None:
    """``auto`` asks one question, and this is it.

    Patched on ``nanopynix.settings``, and not on ``nanopynix._fork``: the
    resolver imported the name, so rebinding it at the source module would not
    reach the caller.
    """
    monkeypatch.setattr("nanopynix.settings.process_is_forked", lambda: forked)
    assert resolve_worker_start("auto") == expected

    # A named method is never resolved against anything.
    assert resolve_worker_start("forkserver") == "forkserver"
    assert resolve_worker_start("spawn") == "spawn"
