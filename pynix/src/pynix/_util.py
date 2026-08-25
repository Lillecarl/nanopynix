from __future__ import annotations

import functools
import json
import sys
import time
from contextlib import asynccontextmanager

# A real import, and not a `TYPE_CHECKING` one: `resolve_local_store_path`
# builds a `Path` at run time.
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TextIO, cast

import anyio
import structlog
from rich.console import Console
from rich.text import Text

import nanopynix
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check

if TYPE_CHECKING or BEARTYPING:
    import os
    from collections.abc import AsyncGenerator, Sequence

    from nanopynix_helpers import EvaluationTargetError

    from nanopynix import AsyncEvalSession, AsyncSession, AsyncStore

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


def error_exit(message: str | Text, *, cause: BaseException | None = None) -> NoReturn:
    """Print ``Error: message`` to stderr and exit(1).

    **The message arrives as a `Text`, and never inside a markup string.**
    Most of these messages come from Nix, which colours its own output, and
    an interpolated escape reaches the highlighter of rich as literal text.
    Measured, with ``ESC`` for the escape byte, on ``ESC[35;1mvalue ...``::

        f"...{message}"           ->  ESC ESC[1m [ ESC[0m ESC[1;36m 35 ESC[0m ;1mvalue ...
        Text.from_ansi(message)   ->  ESC[1;35m value is a string ESC[0m

    ``ReprHighlighter`` matches the ``[`` and the ``35`` and styles each one,
    which leaves the escape byte of Nix with nothing after it. The markup
    parser is not the cause: ``RE_TAGS`` starts a tag at ``[a-z#/@]``, and a
    digit does not match. ``Text.from_ansi`` turns the escape into a style of
    rich before rich sees the text, so a terminal keeps the colour and a pipe
    drops it. A ``Text`` is also never highlighted, which is what makes this
    safe for a message that pynix did not write.

    Pass *cause* to chain the exception, so that a ``--debug`` traceback keeps
    the real reason.
    """
    error_console.print("[red]Error:[/red]", Text.from_ansi(message) if isinstance(message, str) else message)
    raise SystemExit(1) from cause


def report_and_exit(exc: EvaluationTargetError) -> NoReturn:
    """Like `error_exit`, but for an EvaluationTargetError (or subclass, e.g.
    build.py's BuildTargetError) caught from `target.validate()`/
    `evaluate_target()` -- chains `from exc` so --debug tracebacks keep the
    real cause."""
    error_exit(str(exc), cause=exc)


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
    namespace: nanopynix.OverlayNamespace | None = None,
) -> AsyncGenerator[AsyncSession[Any, Any, Any]]:
    """Open a Nix session and forward its logs for the duration of the block.

    Defaults to :class:`nanopynix.inproc.Session`. When *namespace* is given,
    opens :class:`nanopynix.rpc.Session` instead, because entering an overlay
    namespace requires process isolation.
    """
    kwargs: dict[str, Any] = {}
    if settings is not None:
        kwargs["settings"] = settings
    if experimental_features is not None:
        kwargs["experimental_features"] = list(experimental_features)
    if verbosity is not None:
        kwargs["verbosity"] = verbosity
    if namespace is not None:
        kwargs["namespace"] = namespace
        session_factory = nanopynix.rpc.Session
    else:
        session_factory = nanopynix.inproc.Session
    async with (
        session_factory(**kwargs) as nix,
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
) -> AsyncGenerator[tuple[AsyncSession[Any, Any, Any], AsyncStore]]:
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


async def resolve_local_store_path(store: Any, path: str) -> Path:
    """Where *path* really is on this filesystem, for a store that has one.

    A chroot store reports ``storeDir`` as the logical ``/nix/store`` and puts
    the files somewhere else, so the string a caller types is not a path that
    ``open`` can take. ``store_dirs`` reports both, and this is the translation.

    Raises ``SystemExit`` when the store exposes no local path, which is what
    an ssh or an http store does.

    Shared by ``pynix store cat``, ``pynix store ls`` and ``pynix why-depends
    --precise``. It lived in ``pynix._impl.store`` until the third one needed
    it, and a command reaching into the implementation module of another
    command is what this module exists to avoid.

    ``Any`` and not ``AsyncStore``, which is what every ``_impl`` helper that
    takes a store uses. ``NANOPYNIX_BEARTYPING=1`` checks the annotation at
    run time, and ``test_store_gc.py`` drives these two commands through a
    double that answers ``store_dirs`` and ``query_path_info`` and is not an
    instance of the protocol. Typing this parameter tightly failed both of
    those tests with ``BeartypeCallHintParamViolation``.
    """
    dirs = await store.store_dirs()
    store_dir = dirs.store_dir.rstrip("/")
    store_path, suffix = _split_store_path(path, store_dir)
    await store.query_path_info(store_path)

    if dirs.real_store_dir is None:
        raise SystemExit("store does not expose a local filesystem path")
    return Path(dirs.real_store_dir) / store_path.removeprefix(f"{store_dir}/") / suffix


def _split_store_path(path: str, store_dir: str) -> tuple[str, Path]:
    prefix = f"{store_dir}/"
    if not path.startswith(prefix):
        raise SystemExit(f"{path} is not inside {store_dir}")
    rest = path.removeprefix(prefix)
    base_name, separator, suffix = rest.partition("/")
    if not base_name:
        raise SystemExit(f"{path} is not a store path")
    suffix_path = Path(suffix) if separator else Path()
    if suffix_path.is_absolute() or ".." in suffix_path.parts:
        raise SystemExit(f"{path} escapes {store_dir}/{base_name}")
    return f"{store_dir}/{base_name}", suffix_path


@asynccontextmanager
async def eval_session(
    store_uri: str,
    *,
    settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
    experimental_features: Sequence[str] | None = None,
    verbosity: nanopynix.LogLevelInput | None = None,
    print_build_logs: bool = False,
) -> AsyncGenerator[tuple[AsyncSession[Any, Any, Any], AsyncStore, AsyncEvalSession[Any]]]:
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
