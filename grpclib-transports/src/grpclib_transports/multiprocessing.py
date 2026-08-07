"""Multiprocessing pipe-pair helpers: worker contexts and dup'd FDs."""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import os
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

# The submodule, explicitly: anyio's `__init__` does not import `to_thread`,
# so a plain `import anyio` would leave the attribute unbound.
import anyio.to_thread
from grpclib._typing import IServable
from grpclib.encoding.base import StatusDetailsCodecBase

from grpclib_transports.control import WorkerBackchannel, open_parent_control_peer
from grpclib_transports.pipes import PipeChannel, pipe_streams_from_fds
from grpclib_transports.protocol import DEFAULT_TUNING, TransportTuning, serve_h2

ServiceFactory = Callable[[], Collection[IServable]]
BackchannelServiceFactory = Callable[[WorkerBackchannel], Collection[IServable]]
#: What a worker does in the child after `serve_h2` returns, before the child
#: exits. It receives the handlers the factory built, and it runs inside the
#: child's event loop, so it can await a task the worker started there.
#:
#: The forkserver pickles this alongside the factory, so it has to be a
#: module-level function.
ChildTeardown = Callable[[Collection[IServable]], Awaitable[None]]
_PROCESS_CLOSE_TIMEOUT = 3.0
# How long a worker gets to end itself after its channel closes, before the
# parent signals it. `_stop_process` gives the measurement and the reason.
_PROCESS_EXIT_GRACE = 2.0


def get_worker_context(
    method: str = "forkserver",
    *,
    preload: Sequence[str] = (),
) -> Any:
    """Return a ``multiprocessing`` context for *method*, optionally preloading modules.

    ``preload`` applies to ``forkserver`` alone. It is what makes that method
    cheap: the forkserver imports the list once and each worker is a fork of
    that. No other start method has an equivalent, and asking a ``spawn``
    context for one raises :class:`AttributeError`.

    Measured, 5 workers each, preloading one module that imports Nix::

        forkserver     7.7 ms per worker
        spawn         72.1 ms per worker

    **``spawn`` is the one that works in a forked process.**
    ``multiprocessing.forkserver.ForkServer`` carries no pid guard, so a child
    that inherits a running forkserver reaches
    ``os.waitpid(self._forkserver_pid, WNOHANG)`` on a process that is not its
    own child, and ``ensure_running`` raises ``ChildProcessError``. Measured,
    in a forked child::

        spawn        -> start() succeeded
        forkserver   -> ChildProcessError: [Errno 10] No child processes
    """
    context = mp.get_context(method)
    if preload and method == "forkserver":
        context.set_forkserver_preload(list(preload))
    return context


@contextlib.contextmanager
def main_module_not_reexecuted() -> Any:
    """Keep the child from running the program's ``__main__`` again.

    Wrap :meth:`Process.start` with this. ``multiprocessing`` builds the data
    for the child inside ``start()``, and ``spawn.get_preparation_data`` puts
    ``sys.modules["__main__"].__file__`` in it. The child then runs that file
    through ``runpy.run_path`` before it unpickles anything. With no
    ``__file__``, the data names no path and the child skips the step.

    **The child runs the whole top level of whatever program started it, and
    that program is not always a script that expects it.** Measured with
    ``ansible-playbook``, where ``__main__`` imports ``ansible.cli``: that
    import builds a ``Display``, which reads ``sys.stdout``, which is ``None``
    in the child because Ansible gives the forkserver closed standard file
    descriptors. ``ansible/cli/__init__.py`` catches the failure and calls
    ``sys.exit(5)``. A ``SystemExit`` is not an ``Exception``, so the
    ``except Exception`` of the forkserver does not catch it, and the child
    dies through ``os._exit`` with status 1 and prints nothing. The caller
    sees a closed pipe. See nanopynix issue #97.

    **The rule this puts on a caller: the worker payload lives in a module the
    child can import, and never in** ``__main__``. The forkserver pickles the
    service factory and the teardown hook by name, and the child resolves each
    name by import. Re-running ``__main__`` is how ``multiprocessing`` would
    otherwise supply a name that only the script defines, and this gives that
    up on purpose. A factory defined in the calling script then fails in the
    child with ``AttributeError``, which is what ``multiprocessing_example.py``
    did until it moved its factory into ``services.py``.

    The rule costs nanopynix nothing, because everything it sends is already
    module-level: the pipe end, the service factory, the tuning, the
    concurrency limit, the codec and the teardown hook. ``parent_services``,
    which is where a caller's own objects live, never leaves the parent.
    """
    main_module = sys.modules.get("__main__")
    main_path = getattr(main_module, "__file__", None)
    if main_module is None or main_path is None:
        yield
        return
    del main_module.__file__
    try:
        yield
    finally:
        main_module.__file__ = main_path


@dataclass(frozen=True)
class MultiprocessingPipeEndpoint:
    """One end of a multiprocessing pipe pair.

    Call :meth:`open_channel` to create a :class:`~grpclib_transports.pipes.PipeChannel`
    backed by the pipe file descriptors.
    """

    read_connection: Any
    write_connection: Any
    transport_name: str = "multiprocessing"

    async def open_channel(
        self,
        *,
        tuning: TransportTuning = DEFAULT_TUNING,
        status_details_codec: StatusDetailsCodecBase | None = None,
    ) -> PipeChannel:
        read_fd = os.dup(self.read_connection.fileno())
        write_fd = os.dup(self.write_connection.fileno())
        reader, writer, transport = await pipe_streams_from_fds(
            read_fd,
            write_fd,
            transport_name=self.transport_name,
            tuning=tuning,
        )
        return PipeChannel(
            reader,
            writer,
            transport=transport,
            tuning=tuning,
            status_details_codec=status_details_codec,
        )

    def close_connections(self) -> None:
        self.read_connection.close()
        self.write_connection.close()


@dataclass(frozen=True)
class MultiprocessingPipePair:
    """A pair of :class:`MultiprocessingPipeEndpoint` — one for parent, one for child."""

    parent: MultiprocessingPipeEndpoint
    child: MultiprocessingPipeEndpoint
    context: Any

    def close_parent_connections(self) -> None:
        self.parent.close_connections()

    def close_child_connections(self) -> None:
        self.child.close_connections()


def multiprocessing_pipe_pair(
    *,
    context: Any | None = None,
    preload: Sequence[str] = (),
) -> MultiprocessingPipePair:
    """Create a :class:`MultiprocessingPipePair` for parent-child communication.

    If *context* is not given, calls :func:`get_worker_context` with *preload*.
    """
    ctx = context or get_worker_context(preload=preload)
    # Only a forkserver context has this method, so a caller that hands in a
    # `spawn` context must not be asked for it. `get_worker_context` makes the
    # same distinction, and gives the measurement behind it.
    if context is not None and preload and ctx.get_start_method() == "forkserver":
        ctx.set_forkserver_preload(list(preload))
    parent_read, child_write = ctx.Pipe(duplex=False)
    child_read, parent_write = ctx.Pipe(duplex=False)
    return MultiprocessingPipePair(
        parent=MultiprocessingPipeEndpoint(
            read_connection=parent_read,
            write_connection=parent_write,
        ),
        child=MultiprocessingPipeEndpoint(
            read_connection=child_read,
            write_connection=child_write,
        ),
        context=ctx,
    )


async def serve_multiprocessing_endpoint(
    endpoint: MultiprocessingPipeEndpoint,
    handlers: Collection[IServable],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Serve gRPC over a multiprocessing pipe endpoint."""
    reader, _writer, transport = await pipe_streams_from_fds(
        os.dup(endpoint.read_connection.fileno()),
        os.dup(endpoint.write_connection.fileno()),
        transport_name=endpoint.transport_name,
        tuning=tuning,
    )
    endpoint.close_connections()
    await serve_h2(
        tuple(handlers),
        reader,
        transport,
        tuning=tuning,
        max_concurrency=max_concurrency,
        status_details_codec=status_details_codec,
    )


def _run_multiprocessing_worker(
    endpoint: MultiprocessingPipeEndpoint,
    service_factory: ServiceFactory,
    tuning: TransportTuning,
    max_concurrency: int | None,
    status_details_codec: StatusDetailsCodecBase | None = None,
    child_teardown: ChildTeardown | None = None,
) -> None:
    async def run() -> None:
        services = tuple(service_factory())
        await serve_multiprocessing_endpoint(
            endpoint,
            services,
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        )
        # Inside `asyncio.run`, and not after it: a worker's teardown awaits
        # the tasks it started on this loop, and there is no loop left once
        # `asyncio.run` returns.
        if child_teardown is not None:
            await child_teardown(services)

    asyncio.run(run())


def _run_multiprocessing_worker_with_backchannel(
    endpoint: MultiprocessingPipeEndpoint,
    service_factory: BackchannelServiceFactory,
    tuning: TransportTuning,
    max_concurrency: int | None,
    status_details_codec: StatusDetailsCodecBase | None = None,
    child_teardown: ChildTeardown | None = None,
) -> None:
    async def run() -> None:
        backchannel = WorkerBackchannel()
        services = tuple(service_factory(backchannel))
        await serve_multiprocessing_endpoint(
            endpoint,
            (*services, backchannel.service()),
            tuning=tuning,
            max_concurrency=max_concurrency,
            status_details_codec=status_details_codec,
        )
        # `services`, and not the tuple that was served: the backchannel
        # service belongs to this transport, and the teardown is the worker's
        # own. It gets what the factory gave, and nothing else.
        if child_teardown is not None:
            await child_teardown(services)

    asyncio.run(run())


async def _stop_process(proc: Any) -> None:
    # The grace period runs first, and it is not politeness. A worker does its
    # own teardown after `serve_h2` returns, and the parent gets here about
    # 3 ms after it closes the channel. Nothing waited at all until this, so
    # `terminate()` reached a healthy worker while that teardown was still
    # running, and every worker died of SIGTERM with `exitcode` -15. A
    # nanopynix worker measured 51 ms to end itself, so the wait normally costs
    # that and no more.
    #
    # A caller that is already cancelled skips the wait, because the thread
    # hand-off raises at once, and it gets the old behaviour. That is the right
    # trade: a cancelled shutdown asks for speed.
    #
    # `proc.join`, and not a poll on `is_alive()`: a process exit is not an
    # event this loop can await, and the join returns the moment the child
    # ends. It is also the idiom the two calls below already use.
    await anyio.to_thread.run_sync(proc.join, _PROCESS_EXIT_GRACE)

    # `anyio.to_thread.run_sync`, not `asyncio.to_thread`: a pool shutdown
    # stops every worker at once, and asyncio spawns one unbounded thread per
    # call. anyio's shared CapacityLimiter puts a ceiling on that.
    if proc.is_alive():
        proc.terminate()
        await anyio.to_thread.run_sync(proc.join, _PROCESS_CLOSE_TIMEOUT)
    if proc.is_alive():
        proc.kill()
        await anyio.to_thread.run_sync(proc.join, _PROCESS_CLOSE_TIMEOUT)


@contextlib.asynccontextmanager
async def multiprocessing_worker(
    service_factory: ServiceFactory,
    *,
    context: Any | None = None,
    on_process_start: Callable[[Any], None] | None = None,
    preload: Sequence[str] = (),
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
    child_teardown: ChildTeardown | None = None,
) -> AsyncGenerator[PipeChannel]:
    """Start a worker process and yield a gRPC channel to it.

    The start method comes from *context*; see :func:`get_worker_context`.

    ``service_factory`` runs inside the worker process and must return the
    grpclib service handlers served by that worker.

    ``child_teardown`` runs in the worker process after the channel closes,
    and it is the only hook the child has there.
    """
    pair = multiprocessing_pipe_pair(context=context, preload=preload)
    proc = pair.context.Process(
        target=_run_multiprocessing_worker,
        args=(pair.child, service_factory, tuning, max_concurrency, status_details_codec, child_teardown),
    )
    with main_module_not_reexecuted():
        proc.start()
    if on_process_start is not None:
        on_process_start(proc)
    pair.close_child_connections()

    channel = await pair.parent.open_channel(
        tuning=tuning,
        status_details_codec=status_details_codec,
    )
    pair.close_parent_connections()
    try:
        yield channel
    finally:
        await channel.aclose()
        await _stop_process(proc)


@contextlib.asynccontextmanager
async def multiprocessing_worker_with_backchannel(
    service_factory: BackchannelServiceFactory,
    parent_services: Collection[IServable],
    *,
    context: Any | None = None,
    on_process_start: Callable[[Any], None] | None = None,
    preload: Sequence[str] = (),
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
    child_teardown: ChildTeardown | None = None,
) -> AsyncGenerator[PipeChannel]:
    """Start a worker with an in-band parent-services backchannel.

    The start method comes from *context*; see :func:`get_worker_context`.

    The yielded channel lets the parent call services hosted by the worker.
    ``parent_services`` are exposed to the worker over a long-lived
    bidirectional control stream on that same channel.

    ``child_teardown`` runs in the worker process after the channel closes,
    and it is the only hook the child has there.
    """
    pair = multiprocessing_pipe_pair(context=context, preload=preload)
    proc = pair.context.Process(
        target=_run_multiprocessing_worker_with_backchannel,
        args=(pair.child, service_factory, tuning, max_concurrency, status_details_codec, child_teardown),
    )
    with main_module_not_reexecuted():
        proc.start()
    if on_process_start is not None:
        on_process_start(proc)
    pair.close_child_connections()

    channel = await pair.parent.open_channel(
        tuning=tuning,
        status_details_codec=status_details_codec,
    )
    pair.close_parent_connections()
    try:
        async with open_parent_control_peer(channel, parent_services):
            yield channel
    finally:
        await channel.aclose()
        await _stop_process(proc)
