from __future__ import annotations

import functools
import json
import sys
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, NoReturn, TextIO, cast

import anyio
import structlog
from rich.console import Console

import nanopynix
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check

if TYPE_CHECKING or BEARTYPING:
    import os
    from collections.abc import AsyncGenerator, Sequence

    from nanopynix_helpers import EvaluationTargetError

    from nanopynix.rpc import EvalSession, Session, Store

_RESULT_BUILD_LOG_LINE = 101
_RESULT_POST_BUILD_LOG_LINE = 107
# Ceiling on the exit drain, and the idle window that normally ends it well
# before that -- see _drain_logs.
_LOG_DRAIN_SECONDS = 0.5
_LOG_DRAIN_QUIET_SECONDS = 0.05
# A Nix "result" action's args are at least [_, result_type], with an
# optional trailing fields list.
_MIN_RESULT_EVENT_ARGS = 2

# Build-progress/error messages go here, not stdout -- so a command's
# print_json() output stays clean, machine-parseable JSON even when the same
# invocation also fails partway through (e.g. `pynix derivation show ... |
# jq` must never see "Error: ..." text mixed into its stdin).
error_console = Console(stderr=True)


def print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")


def error_exit(message: str) -> NoReturn:
    """Print `[red]Error:[/red] message` to stderr and exit(1)."""
    error_console.print(f"[red]Error:[/red] {message}")
    raise SystemExit(1)


def report_and_exit(exc: EvaluationTargetError) -> NoReturn:
    """Like `error_exit`, but for an EvaluationTargetError (or subclass, e.g.
    build.py's BuildTargetError) caught from `target.validate()`/
    `evaluate_target()` -- chains `from exc` so --debug tracebacks keep the
    real cause."""
    error_console.print(f"[red]Error:[/red] {exc}")
    raise SystemExit(1) from exc


@no_runtime_type_check  # file is duck-typed against TextIO -- under prompt_toolkit's
# patch_stdout(), sys.stdout is swapped for a StdoutProxy that implements the
# file-like write interface without being a real TextIO instance.
def configure_logging(*, file: TextIO | None = None) -> None:
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=file or sys.stderr),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.KeyValueRenderer(sort_keys=True),
        ],
    )


@asynccontextmanager
@no_runtime_type_check  # log_file is duck-typed against TextIO -- see configure_logging above.
async def forward_nix_logs(
    session: Any,
    *,
    print_build_logs: bool = False,
    log_file: TextIO | None = None,
) -> AsyncGenerator[None]:
    old_config = structlog.get_config()
    if log_file is None:
        configure_logging()
    else:
        configure_logging(file=log_file)
    activity = _LogActivity()
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(
                    _forward_nix_logs,
                    session,
                    print_build_logs=print_build_logs,
                    activity=activity,
                )
            )
            try:
                yield
            finally:
                # Shielded: the drain must run to completion even if the
                # caller's block was cancelled, matching the original
                # asyncio (edge-cancellation) behavior of this cleanup.
                with anyio.CancelScope(shield=True):
                    await _drain_logs(activity)
                tg.cancel_scope.cancel()
    except* BaseException as eg:
        # anyio task groups always wrap exceptions in a group, even a lone
        # one raised by the yielded body itself -- unwrap the common single
        # exception case so callers keep seeing the original exception type
        # (e.g. SystemExit) instead of a BaseExceptionGroup.
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None
        raise
    finally:
        structlog.configure(**old_config)


@asynccontextmanager
async def nix_session(
    *,
    settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
    experimental_features: Sequence[str] | None = None,
    verbosity: nanopynix.LogLevelInput | None = None,
    print_build_logs: bool = False,
) -> AsyncGenerator[Session]:
    """Open an RPC session and forward its Nix logs for the duration of the block.

    Only forwards settings/experimental_features/verbosity to ``Session`` when
    the caller actually overrides them, so this degrades to the plain
    ``Session()`` call most command modules use (also keeps this a
    faithful drop-in for tests that stub ``nanopynix.rpc.Session`` with a
    zero-argument fake).
    """
    kwargs: dict[str, Any] = {}
    if settings is not None:
        kwargs["settings"] = settings
    if experimental_features is not None:
        kwargs["experimental_features"] = list(experimental_features)
    if verbosity is not None:
        kwargs["verbosity"] = verbosity
    async with (
        nanopynix.rpc.Session(**kwargs) as nix,
        forward_nix_logs(nix, print_build_logs=print_build_logs),
    ):
        yield nix


@asynccontextmanager
async def store_session(
    store_uri: str,
    *,
    settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
    experimental_features: Sequence[str] | None = None,
    verbosity: nanopynix.LogLevelInput | None = None,
    print_build_logs: bool = False,
) -> AsyncGenerator[tuple[Session, Store]]:
    """Open a session and store, forwarding logs for the duration of the block."""
    async with (
        nix_session(
            settings=settings,
            experimental_features=experimental_features,
            verbosity=verbosity,
            print_build_logs=print_build_logs,
        ) as nix,
        nix.store(store_uri) as store,
    ):
        yield nix, store


@asynccontextmanager
async def eval_session(
    store_uri: str,
    *,
    settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
    experimental_features: Sequence[str] | None = None,
    verbosity: nanopynix.LogLevelInput | None = None,
    print_build_logs: bool = False,
) -> AsyncGenerator[tuple[Session, Store, EvalSession]]:
    """Open a session, store, and eval session, forwarding logs for the duration of the block."""
    async with (
        store_session(
            store_uri,
            settings=settings,
            experimental_features=experimental_features,
            verbosity=verbosity,
            print_build_logs=print_build_logs,
        ) as (nix, store),
        nix.eval(store) as session,
    ):
        yield nix, store, session


class _LogActivity:
    """Counts events seen on the log stream, so the drain can tell it apart
    from a stream that has gone quiet."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


async def _drain_logs(activity: _LogActivity) -> None:
    """Wait for trailing log events, stopping once the stream goes quiet.

    Every log event the worker emits while serving a call has already been sent
    by the time that call's reply arrives, so once the caller's block finishes
    there is essentially nothing left to wait for: measured across eval,
    instantiate, a 56-event build, store queries and a failing eval, exactly one
    event (the request-finalized marker) arrived after the block exited, and
    always within 0.4ms.

    This used to be a flat ``sleep(_LOG_DRAIN_SECONDS)``, which charged every
    pynix invocation the whole ceiling -- 0.49s of measured dead time per
    command, paid once per CLI run and once per test that runs one. Waiting for
    an idle window instead keeps the guarantee (a stream still producing events
    keeps extending, up to the same ceiling) at ~50ms in the normal case.
    """
    deadline = time.monotonic() + _LOG_DRAIN_SECONDS
    seen = -1
    while seen != activity.count and time.monotonic() < deadline:
        seen = activity.count
        await anyio.sleep(_LOG_DRAIN_QUIET_SECONDS)


async def _forward_nix_logs(session: Any, *, print_build_logs: bool, activity: _LogActivity) -> None:
    logger = structlog.get_logger("pynix.nix")
    async for event in session.log_stream():
        # Counted before any filtering: a skipped event still proves the stream
        # is live, which is all the drain needs to know.
        activity.count += 1
        if event.is_request_finalized:
            continue
        if not event.is_nix_log:
            continue
        message = event.message_without_ansi
        result_type, result_message = _result_event(event)
        if event.action == "stop" or (event.action == "result" and result_message is None):
            continue
        if result_type in {_RESULT_BUILD_LOG_LINE, _RESULT_POST_BUILD_LOG_LINE}:
            if print_build_logs:
                logger.info(
                    "nix build log",
                    message=result_message,
                    request_id=event.request_id,
                    result_type=result_type,
                )
            continue
        if event.action == "error":
            logger.error("nix log", message=message, action=event.action, request_id=event.request_id)
        elif event.action == "warn":
            logger.warning("nix log", message=message, action=event.action, request_id=event.request_id)
        else:
            logger.info(
                "nix log",
                message=message,
                action=event.action,
                request_id=event.request_id,
                result_type=event.result_type.name if event.result_type else None,
            )


def _result_event(event: Any) -> tuple[int | None, str | None]:
    if event.action != "result":
        return None, None
    args = event.args
    if len(args) < _MIN_RESULT_EVENT_ARGS or not isinstance(args[1], int):
        return None, None
    result_type = args[1]
    fields: list[Any] = cast("list[Any]", args[2]) if len(args) > _MIN_RESULT_EVENT_ARGS else []
    message = None
    for field in reversed(fields):
        if isinstance(field, str):
            message = field
            break
    return result_type, message
