"""Subprocess worker — Nix execution over gRPC (multiprocessing pipe or stdio).

Spawned by ``Session._WorkerManager`` via ``asyncio.create_subprocess_exec``
(multiprocessing mode) or via forkserver ``Process`` (grpclib-transports mode).
Serves three gRPC services over a single H2 transport:

- ``WorkerService``  — Init (bootstrap Nix), SubscribeLogs (server-streaming), Shutdown
- ``StoreService``   — all store operations
- ``EvalService``    — all eval/flake operations
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
import traceback
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, cast

from nanopynix_proto.nix.common import LogEvent
from nanopynix_proto.nix.worker import (
    InitRequest,
    InitResponse,
    ShutdownRequest,
    ShutdownResponse,
    SubscribeLogsRequest,
    WorkerServiceBase,
)

import nanopynix_expr
import nanopynix_store
import nanopynix_util
from nanopynix._grpc_util import wrap_service_handlers
from nanopynix._manager import LogAck
from nanopynix._worker_eval import EvalServiceHandler
from nanopynix._worker_store import StoreServiceHandler
from nanopynix.logging import LogCollector
from nanopynix.models import PrimOpSpec

if TYPE_CHECKING:
    from grpclib._typing import IServable
    from grpclib_transports import WorkerBackchannel

# Re-export for the multiprocessing runner in _pool.py
__all__ = ["run_worker", "worker_service_factory", "main"]

# Current worker logging uses the long-lived grpclib-transports backchannel on
# the same H2 connection as ordinary calls. Keep one handler slot for that stream
# and one for the active manager->worker call. EvalSession still serializes Nix
# eval RPCs at the ReservedWorker boundary.
_WORKER_MAX_CONCURRENCY = 2


# ── Primop registration ──────────────────────────────────────────────


def _import_callable(import_path: str) -> Callable[..., Any]:
    module_name, sep, attr_path = import_path.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(f"invalid primop import path: {import_path!r}")
    value: Any = importlib.import_module(module_name)
    for attr in attr_path.split("."):
        value = getattr(value, attr)
    if not callable(value):
        raise TypeError(f"primop import path is not callable: {import_path!r}")
    return value


def _register_primops(raw_specs: list[dict[str, Any]]) -> None:
    for raw in raw_specs:
        spec = PrimOpSpec.from_dict(raw)
        nanopynix_expr.register_primop(
            spec.name,
            spec.arity,
            spec.args,
            spec.doc,
            _import_callable(spec.import_path),
        )


# ── Worker state ─────────────────────────────────────────────────────


class WorkerState:
    """Shared mutable state held by all three service handlers.

    Initialized by ``WorkerServiceHandler.init()`` (bootstraps Nix).
    """

    def __init__(self) -> None:
        self.store: Any = None
        self.eval_state: Any = None
        self.collector: LogCollector | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.locked_flakes: dict[int, Any] = {}
        self._next_lf_handle: int = 1


# ── WorkerService handler ────────────────────────────────────────────


@wrap_service_handlers
class WorkerServiceHandler(WorkerServiceBase):
    """Lifecycle handler: init, subscribe-logs, shutdown."""

    def __init__(self, state: WorkerState) -> None:
        self._state = state

    async def init(self, message: InitRequest) -> InitResponse:
        """Bootstrap Nix: configure, init libstore/libexpr, open store."""
        try:
            # Apply config file path before init
            if message.nix_conf is not None:
                os.environ["NIX_USER_CONF_FILES"] = message.nix_conf
            if message.settings:
                os.environ["NIX_CONFIG"] = "\n".join(
                    f"{k} = {v}" for k, v in message.settings.items()
                )

            for k, v in message.settings.items():
                nanopynix_util.set_setting(k, v)
            for f in message.experimental_features:
                nanopynix_util.enable_experimental_feature(f)

            nanopynix_util.init_libstore(load_config=False)
            nanopynix_util.set_verbosity(5)  # lvlChatty — emit fetch/log events
            nanopynix_expr.init_libexpr()

            # Convert proto PrimOpSpec list to the raw-dict format
            primops_raw = [
                {
                    "name": p.name,
                    "arity": p.arity,
                    "args": list(p.args),
                    "doc": p.doc,
                    "import_path": p.import_path,
                }
                for p in message.primops
            ]
            _register_primops(primops_raw)

            store_uri = message.store_uri

            self._state.store = (
                nanopynix_store.open_store()
                if store_uri == "auto"
                else nanopynix_store.open_store(store_uri)
            )

            return InitResponse(status="ok")

        except Exception:
            traceback.print_exc(file=sys.stderr)
            # Re-raise so the gRPC framework propagates the error.
            raise

    async def subscribe_logs(
        self, message: SubscribeLogsRequest
    ) -> AsyncIterator[LogEvent]:
        """Server-streaming RPC — yield log events as they arrive."""
        collector = self._state.collector
        if collector is None:
            return

        try:
            async for event in collector.stream():
                if event is None:
                    break
                req_id, action, *args = event
                yield LogEvent(
                    request_id=req_id,
                    action=action,
                    args_json=json.dumps(list(args), default=str),
                )
        except asyncio.CancelledError:
            pass

    async def shutdown(self, message: ShutdownRequest) -> ShutdownResponse:
        """Acknowledge shutdown request.

        The actual process exit happens when the transport / pipe closes.
        """
        collector = self._state.collector
        if collector is not None:
            collector.close()
        return ShutdownResponse()


# ── Factory ──────────────────────────────────────────────────────────


async def _relay_logs_to_manager(collector: LogCollector, backchannel: WorkerBackchannel) -> None:
    try:
        async for event in collector.stream():
            if event is None:
                break
            req_id, action, *args = event
            await backchannel.call_unary(
                "/nix.manager.ManagerService/Log",
                LogEvent(
                    request_id=req_id,
                    action=action,
                    args_json=json.dumps(list(args), default=str),
                ),
                LogAck,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        traceback.print_exc(file=sys.stderr)


def worker_service_factory(backchannel: WorkerBackchannel | None = None) -> list[IServable]:
    """Create service handlers with a shared WorkerState.

    Must be called *inside* the worker process (before Nix init so that
    the logger is installed early).
    """
    collector = LogCollector()
    nanopynix_util.install_logger(collector.callback)

    state = WorkerState()
    state.collector = collector
    if backchannel is not None:
        state.log_task = asyncio.create_task(
            _relay_logs_to_manager(collector, backchannel),
            name="nanopynix-log-backchannel",
        )

    return cast(
        "list[IServable]",
        [
            WorkerServiceHandler(state),
            StoreServiceHandler(state),
            EvalServiceHandler(state),
        ],
    )


# ── Async runner (for grpclib-transports multiprocessing mode) ───────


async def run_worker(
    endpoint: Any,
    tuning: Any = None,
    max_concurrency: int | None = _WORKER_MAX_CONCURRENCY,
) -> None:
    """Serve gRPC over a multiprocessing pipe endpoint.

    Called by the forkserver child process via ``_run_multiprocessing_worker``
    in grpclib_transports.
    """
    from grpclib_transports.multiprocessing import serve_multiprocessing_endpoint
    from grpclib_transports.protocol import DEFAULT_TUNING

    tuning = tuning or DEFAULT_TUNING
    handlers = worker_service_factory()
    await serve_multiprocessing_endpoint(
        endpoint,
        handlers,
        tuning=tuning,
        max_concurrency=max_concurrency,
    )

    # Cleanup after transport closes
    worker_handler = handlers[0]
    collector = cast("WorkerServiceHandler", worker_handler)._state.collector
    if collector is not None:
        collector.close()
    log_task = cast("WorkerServiceHandler", worker_handler)._state.log_task
    if log_task is not None:
        log_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_task


# ── Stdio entry point (console_script / ``python -m nanopynix._worker``) ──


def main() -> None:
    """Stdio entry point — serves gRPC over stdin/stdout.

    This is the ``nanopynix-worker`` console_script target and also the
    fallback for ``sys.executable -m nanopynix._worker``.
    """
    try:
        asyncio.run(_stdio_main())
    except KeyboardInterrupt:
        pass


async def _stdio_main() -> None:
    from grpclib_transports.stdio import serve_stdio

    handlers = worker_service_factory()
    await serve_stdio(handlers, max_concurrency=_WORKER_MAX_CONCURRENCY)

    # Cleanup after transport closes
    worker_handler = handlers[0]
    collector = cast("WorkerServiceHandler", worker_handler)._state.collector
    if collector is not None:
        collector.close()
    log_task = cast("WorkerServiceHandler", worker_handler)._state.log_task
    if log_task is not None:
        log_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await log_task


if __name__ == "__main__":
    main()
