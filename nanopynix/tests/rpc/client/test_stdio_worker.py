"""A session whose worker started by ``exec``, over its stdin and stdout.

``worker_start="stdio"`` is the one start method with no ``multiprocessing``
in it: it execs ``python -m nanopynix.rpc.worker``. That means a different
transport, a different way to reach the process, and a worker whose services
are built by ``_stdio_main`` rather than by a factory the parent pickled. This
module holds each thing that difference could break.

**The RPC primop is the load-bearing one.** ``_stdio_main`` called the plain
``serve_stdio`` until issue #25, so the worker served no backchannel and a
primop had no route back to the client at all.

The rest of the rpc suite runs on ``forkserver``, and deliberately: a stdio
worker costs about 900 ms to start against 49 ms for a warm forkserver, so
this is a chosen subset rather than a parametrisation of everything.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from grpclib_transports.stdio import stdio_worker_with_backchannel
from nanopynix_proto.nix.worker import InitRequest, ShutdownRequest, WorkerServiceStub

from nanopynix.models import PrimOpSpec
from nanopynix.rpc import WorkerDiedError, WorkerSignaledError
from nanopynix.rpc._status_details import NIX_STATUS_DETAILS_CODEC
from nanopynix.rpc._worker_argv import worker_argv
from nanopynix.settings import NanopynixSettings
from nanopynix_testing.nix_markers import LINUX_PROC_FS
from nanopynix_testing.worker_death import expect_the_worker_to_die

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import NixTestEnvironment, RpcSessionFactory

STDIO = NanopynixSettings(worker_start="stdio")

_WORKER_DEATH_DEADLINE = 30.0
"""How long the signalled worker gets to be noticed.

Generous against the work, which is one signal and one closed pipe. It bounds
a hang; it is not a performance assertion."""


def _rpc_double(x: int) -> int:
    return x * 2


async def test_a_stdio_worker_answers_store_operations(rpc_session: RpcSessionFactory) -> None:
    """The first thing that would fail if the transport were wrong at all."""
    async with rpc_session(runtime_settings=STDIO) as nix, nix.store() as store:
        assert isinstance(await store.uri(), str)
        assert await store.store_dir() == "/nix/store"


async def test_a_stdio_worker_evaluates(rpc_session: RpcSessionFactory) -> None:
    """An evaluator lives on a thread the worker starts after it enters serve.

    Nothing about that is transport-specific, which is the point: it proves
    the stdio worker reached the same state a forkserver worker reaches.
    """
    async with (
        rpc_session(runtime_settings=STDIO) as nix,
        nix.store() as store,
        nix.eval(store) as evaluator,
    ):
        assert await (await evaluator.string("1 + 1")).as_int() == 2


async def test_an_rpc_primop_reaches_the_client_over_stdio(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The backchannel, which ``_stdio_main`` did not serve before issue #25.

    An RPC primop is Nix calling back into the client process, so it uses the
    control stream that ``serve_stdio_with_backchannel`` opens and the plain
    ``serve_stdio`` does not. Without it ``WorkerServiceHandler.init`` refuses
    to register the primop at all, and this fails at session open rather than
    at evaluation.
    """
    spec = PrimOpSpec(
        name="stdioDouble",
        arity=1,
        args=["x"],
        doc="RPC primop over the stdio backchannel",
        rpc=True,
    )
    async with (
        shared_nix_environment.rpc_session(
            runtime_settings=STDIO,
            primops=[spec],
            primop_callables={"stdioDouble": _rpc_double},
        ) as nix,
        nix.store() as store,
        nix.eval(store) as evaluator,
    ):
        assert await (await evaluator.string("builtins.stdioDouble 21")).as_int() == 42


async def test_log_events_reach_the_client_over_stdio(rpc_session: RpcSessionFactory) -> None:
    """``SubscribeLogs`` is a server-streaming RPC, and streams are where a
    transport with a broken flow-control window fails rather than at the first
    unary call."""
    events: list[Any] = []

    async with rpc_session(runtime_settings=STDIO) as nix:
        nix.subscribe(events.append)
        async with nix.store() as store, nix.eval(store) as evaluator:
            await evaluator.string('builtins.trace "over the wire" 1')

    assert any(event is not None for event in events)


async def test_the_worker_ends_itself_and_reports_status_zero(rpc_session: RpcSessionFactory) -> None:
    """A clean close leaves 0, and not -15.

    Two things have to hold for that. ``_close_worker_process`` has to wait
    before it signals -- it did not, so every stdio worker used to die of
    SIGTERM with its teardown unrun -- and ``_stdio_main`` has to run
    ``_shutdown_worker`` after the transport closes, which is what unregisters
    each evaluator thread from the Boehm collector.
    """
    nix = rpc_session(runtime_settings=STDIO)
    await nix.open()
    proc = nix._manager._worker_proc  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
    assert proc is not None

    await nix.close()

    assert proc.exit_status == 0, f"the stdio worker was signalled rather than left to end itself: {proc.exit_status}"


@LINUX_PROC_FS
async def test_worker_oom_score_adj_reaches_the_exec_d_process(rpc_session: RpcSessionFactory) -> None:
    """``on_process_start`` is the only route to the pid, and this uses it.

    ``stdio_worker`` yields a channel and says nothing about the peer, so
    without that hook the setting would silently do nothing on this start
    method while working on every other one.
    """
    async with rpc_session(runtime_settings=STDIO, worker_oom_score_adj=250) as nix:
        pid = nix._manager._worker_pid  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
        assert pid is not None
        adjustment = await anyio.Path(f"/proc/{pid}/oom_score_adj").read_text()
        assert adjustment.strip() == "250"


async def test_a_signalled_stdio_worker_is_reported_and_not_swallowed(rpc_session: RpcSessionFactory) -> None:
    """The exit status is what tells an abort from an exit, on this transport too.

    ``asyncio.subprocess.Process.returncode`` reports a signal as a negative
    number, exactly as ``multiprocessing.Process.exitcode`` does, so
    ``WorkerSignaledError`` needs no second convention. This is the test that
    the two really do agree.
    """
    nix = rpc_session(runtime_settings=STDIO)
    async with nix:
        await nix.get_verbosity()
        pid = nix._manager._worker_pid  # type: ignore[reportPrivateUsage] -- intentional test of internal transport state
        assert pid is not None

        expect_the_worker_to_die(nix)
        os.kill(pid, signal.SIGKILL)

        # The call still fails, and still raises. `expect_the_worker_to_die`
        # gates the close alone -- see tests/AGENTS.md.
        death = await _the_call_that_notices_the_death(nix)

    # The upgrade is the subject: `WorkerDiedError` alone means "the pipe
    # broke", and only the exit status says a signal did it.
    assert isinstance(death, WorkerSignaledError)
    assert death.exit_status == -signal.SIGKILL


async def _the_call_that_notices_the_death(nix: Any) -> WorkerDiedError:
    """Call until the closed pipe surfaces, and return what that call raised.

    A loop, because a request may already have been on the wire when the
    signal landed, and that one can answer before the transport notices. A
    deadline, because a death that never surfaces is the failure this test is
    about and it must not become a hang.
    """
    with anyio.fail_after(_WORKER_DEATH_DEADLINE):
        while True:
            try:
                await nix.get_verbosity()
            except WorkerDiedError as exc:
                return exc


async def test_the_console_script_serves_a_worker(tmp_path: Path) -> None:
    """``nanopynix-worker`` is a shipped entry point, and nothing tested it.

    ``grpclib_transports.ssh.connect_ssh_stdio`` runs this script on a remote
    host, so it is the far end of the one SSH mode that reaches a machine
    running OpenSSH. This starts it the same way, locally, and speaks the
    protocol to it.

    Started from the argument vector rather than from the script name on
    ``PATH``, because the two are the same program and the vector is the one
    the client actually uses.
    """
    async with stdio_worker_with_backchannel(
        worker_argv(),
        [],
        status_details_codec=NIX_STATUS_DETAILS_CODEC,
    ) as channel:
        stub = WorkerServiceStub(channel)
        response = await stub.init(
            InitRequest(request_id=1, store_uri=f"local?root={tmp_path}", load_config=False),
            timeout=120.0,
        )
        assert response.status == "ok"
        await stub.shutdown(ShutdownRequest(request_id=2), timeout=30.0)


async def test_the_console_script_is_installed_beside_this_interpreter() -> None:
    """The name ``connect_ssh_stdio`` runs on the far host exists here too.

    The test above proves the program works; this proves the name it ships
    under still points at it. ``pyproject.toml`` declares the console script,
    and nothing else would notice if the entry point were renamed.
    """
    script = anyio.Path(sys.executable).parent / "nanopynix-worker"
    assert await script.exists(), f"the nanopynix-worker console script is not beside {sys.executable}"

    proc = await asyncio.create_subprocess_exec(
        str(script),
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    finally:
        if proc.returncode is None:  # pragma: no cover -- only on a hang
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()

    assert proc.returncode == 0
    assert b"--namespace" in stdout
