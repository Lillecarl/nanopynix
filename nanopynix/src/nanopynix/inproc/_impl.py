"""Asynchronous in-process Nix API backed by direct L1 object pointers.

Unlike :class:`nanopynix.rpc.Session`, this module does not start a worker
process. Store work uses a bounded thread pool, while each evaluator owns a
dedicated thread so callers retain an asynchronous API without exposing Nix's
evaluator thread-affinity requirements.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import math
import os
import threading
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from nanopynix_bindings import expr as nanopynix_expr, store as nanopynix_store, util as nanopynix_util
from nanopynix_proto.nix.common import LogLevel, RequestFinalized

from nanopynix._core._extract import locked_flake as _locked_flake_proto
from nanopynix._core._nix_core import build_mode_value
from nanopynix._core._nix_executor import NIX_EVALUATOR_STACK_SIZE, NixThreadExecutor
from nanopynix._core._objects import CoreEvalState, CoreLockedFlake, CoreRuntime, CoreStore, CoreValue
from nanopynix._core._primops import register_import_path_primops, to_primop_specs
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix._wire import (
    DEFAULT_CA_METHOD,
    DEFAULT_HASH_ALGO,
    DEFAULT_STORE_URI,
    NIX_USER_CONF_FILES_ENV,
    NO_GC_LIMIT,
)
from nanopynix.exceptions import (
    EvalSessionClosedError,
    LockedFlakeReleasedError,
    SessionClosedError,
    StoreClosedError,
    StoreError,
    ValueReleasedError,
    build_error_from_result,
    translate_nix_exception,
)
from nanopynix.logging import (
    ACTIVE_LOG_CAPTURES,
    BusSubscription,
    CallbackBus,
    LogCapture,
    LogCollector,
    LogStreamEventKind,
)
from nanopynix.models import (
    BuildResult,
    Derivation,
    FlakeRef,
    GcResult,
    GcRoot,
    LockedInput,
    LogEvent,
    MissingInfo,
    NixType,
    PathInfo,
    StorePath,
)
from nanopynix.settings import (
    DEFAULT_LINE_EDITORS,
    NIX_PATH_SETTING_KEY,
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
    NixGlobalSettings,
    NixSettings,
    NixStoreDefaults,
    SettingsProvenance,
    merge_defaults,
    normalize_nix_path,
    normalize_nix_settings,
    reject_construction_time_keys,
    reject_settings_write_while_open,
)
from nanopynix.stores import StoreConfig, resolve_store_spec
from nanopynix.verbosity import LogLevelInput, normalize_log_level

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from os import PathLike

    from nanopynix_proto.nix.store import GcAction, StoreDirs

    from nanopynix.models import PrimOpSpec


BuildMode = nanopynix_store.BuildMode
RawEvalState = nanopynix_expr.EvalState
RawGCAction = nanopynix_store.GCAction
RawStore = nanopynix_store.Store
RawStorePath = nanopynix_store.StorePath
RawValue = nanopynix_expr.Value


class _InprocProcessGuard:
    """Reflect the irreducible process-global portion of an in-process Nix runtime.

    Nix library initialization cannot be undone, so its initial configuration
    remains relevant after an :class:`Session` closes. The Python executor and
    every direct Nix object remain session-owned.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_session: Session | None = None
        self._initialization_signature: tuple[object, ...] | None = None

    def acquire(self, session: Session, signature: tuple[object, ...]) -> None:
        with self._lock:
            active = self._active_session
            if active is not None and active is not session:
                raise RuntimeError("only one nanopynix.inproc.Session may be open per process")
            initialized = self._initialization_signature
            if initialized is not None and signature != initialized:
                raise RuntimeError(
                    "Nix is already initialized in this process with different nanopynix.inproc settings"
                )
            self._active_session = session

    def mark_initialized(self, signature: tuple[object, ...]) -> None:
        with self._lock:
            if self._initialization_signature is None:
                self._initialization_signature = signature

    def release(self, session: Session) -> None:
        with self._lock:
            if self._active_session is session:
                self._active_session = None


_process_guard = _InprocProcessGuard()


def _print_store_path(raw_store: nanopynix_store.Store, raw_path: nanopynix_store.StorePath | str) -> str:
    """Render a store path absolute, whichever of Nix's two spellings arrives.

    The union is not convenience: the bindings genuinely return both. Anything
    that goes through ``parse_store_path`` hands back a ``RawStorePath``, whose
    ``str()`` is the bare ``hash-name``, while the collective queries funnel
    through C++'s ``store_paths_to_string_list`` and hand back strings that are
    already absolute. Normalising both here is what lets one helper serve
    either; the prefix test below is what makes it idempotent.
    """
    path = str(raw_path)
    store_dir = raw_store.get_store_dir().rstrip("/")
    if path == store_dir or path.startswith(f"{store_dir}/"):
        return path
    return f"{store_dir}/{path}"


def _print_store_paths(
    raw_store: nanopynix_store.Store, raw_paths: Sequence[nanopynix_store.StorePath | str]
) -> list[str]:
    return [_print_store_path(raw_store, path) for path in raw_paths]


def _run_with_log_context[T](operation_id: int, func: Callable[..., T], args: tuple[object, ...]) -> T:
    """Run one Nix call on the Nix thread, in this operation's log context.

    This is the single chokepoint through which every in-process Nix call
    passes, so it is also where boundary A is translated: the raw nanobind
    exception is replaced by its ``nanopynix.exceptions`` equivalent, giving
    inproc callers the same exception types the rpc engine raises.
    """
    previous = nanopynix_util.get_logger_request_id()
    nanopynix_util.set_logger_request_id(operation_id)
    try:
        return func(*args)
    except Exception as exc:
        translated = translate_nix_exception(exc)
        if translated is None:
            raise
        raise translated from exc
    finally:
        nanopynix_util.set_logger_request_id(previous)


class Session:
    """Own one asynchronous, pointer-backed in-process Nix runtime.

    Nix library initialization and logger installation are process-global. To
    make that constraint explicit, only one :class:`Session` may be open at a
    time. Store work uses a bounded pool and each live :class:`EvalSession`
    owns one separate Nix thread.
    """

    @no_runtime_type_check  # nix_conf validates its own type at runtime for untyped
    # callers (see the isinstance guard below); beartype's parameter check
    # would otherwise intercept before that guard runs and raise its own
    # exception type instead of the documented TypeError.
    def __init__(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
        self,
        *,
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: NixSettings | PathLike[str] | str | None = None,
        experimental_features: Sequence[str] | None = None,
        verbosity: LogLevelInput | None = None,
        nix_path: str | Sequence[str] | None = None,
        primops: Sequence[PrimOpSpec | Mapping[str, Any]] | None = None,
        store_uri: StoreConfig | str = DEFAULT_STORE_URI,
        store_workers: int = 4,
    ) -> None:
        if nix_conf is not None:
            if not isinstance(nix_conf, Path):  # type: ignore[reportUnnecessaryIsInstance] -- runtime guard for untyped callers
                raise TypeError("nix_conf must be a pathlib.Path or None")
            if not nix_conf.exists():
                raise FileNotFoundError(nix_conf)
        self._nix_conf = nix_conf
        self._load_config = load_config
        self._settings = normalize_nix_settings(settings).with_experimental_features(list(experimental_features or []))
        self._verbosity = normalize_log_level(verbosity) if verbosity is not None else None
        self._nix_path = normalize_nix_path(nix_path)
        self._primops = to_primop_specs(primops)
        # One scope for each door Nix opens -- see the same block in
        # rpc.client.session.Session.__init__, which must agree with this one.
        self._global_settings = NixGlobalSettings.for_scope(self._settings)
        self._store_defaults = NixStoreDefaults.for_scope(self._settings)
        self._eval_defaults = NixEvalSettings.for_scope(self._settings)
        self._fetch_defaults = NixFetchSettings.for_scope(self._settings)
        self._flake_defaults = NixFlakeSettings.for_scope(self._settings)
        self._store_uri = resolve_store_spec(store_uri, self._store_defaults)
        if store_workers < 1:
            raise ValueError("store_workers must be at least 1")
        self._store_workers = store_workers
        self._collector = LogCollector()
        self._runtime = CoreRuntime()
        #: Where this session's configuration came from, recorded by `open`.
        self._provenance = SettingsProvenance()
        # Creation is deliberately lazy: merely importing nanopynix.inproc
        # must not start a Nix thread in an L3 manager process.
        self._executor: NixThreadExecutor | None = None
        self._log_bus = CallbackBus()
        self._log_task: asyncio.Task[None] | None = None
        self._opened = False
        self._operation_ids = itertools.count(1)
        self._evals: set[EvalSession] = set()
        self._stores: set[Store] = set()
        # What `NIX_USER_CONF_FILES` held before `open` set it. `None` is a
        # real value here -- "the process did not have it" -- so the flag is
        # separate rather than encoded into the same attribute.
        self._nix_conf_env_saved = False
        self._nix_conf_env_before: str | None = None

    async def __aenter__(self) -> Session:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._opened:
            return
        signature = self._initialization_signature()
        _process_guard.acquire(self, signature)
        executor = NixThreadExecutor(max_workers=self._store_workers, thread_name_prefix="nix-store")
        self._executor = executor
        logger_installed = False
        try:
            if self._nix_conf is not None:
                self._nix_conf_env_before = os.environ.get(NIX_USER_CONF_FILES_ENV)
                self._nix_conf_env_saved = True
                os.environ[NIX_USER_CONF_FILES_ENV] = str(self._nix_conf)
            nanopynix_util.install_logger(self._collector.callback)
            logger_installed = True
            await executor.run(self._init_nix)
            _process_guard.mark_initialized(signature)
            self._opened = True
            self._log_task = asyncio.create_task(self._forward_logs())
        except BaseException:
            try:
                if logger_installed:
                    await executor.run(nanopynix_util.remove_logger)
            finally:
                try:
                    executor.shutdown(wait=True)
                finally:
                    self._executor = None
                    self._restore_nix_conf_env()
                    _process_guard.release(self)
            raise

    def _restore_nix_conf_env(self) -> None:
        """Put ``NIX_USER_CONF_FILES`` back the way ``open`` found it.

        The variable is an input Nix reads once, in ``loadConfFile``, while
        ``initialize`` runs. Nothing looks at it again, so putting it back
        costs the session nothing.

        The RPC worker sets the same variable and never restores it, and there
        that is correct: the worker process is disposable. This process is not.
        Leaving the assignment behind changed the environment of the whole
        program for every later use of Nix in it, including one by a library
        that never opened a session.
        """
        if not self._nix_conf_env_saved:
            return
        self._nix_conf_env_saved = False
        previous = self._nix_conf_env_before
        self._nix_conf_env_before = None
        if previous is None:
            os.environ.pop(NIX_USER_CONF_FILES_ENV, None)
        else:
            os.environ[NIX_USER_CONF_FILES_ENV] = previous

    def _initialization_signature(self) -> tuple[object, ...]:
        # The globals only, because process-global state is all this guard
        # guards. Two sessions that differ in an eval or a fetch setting are
        # compatible: those live on each `EvalState`, not on the process, so
        # refusing them was over-strict. The store defaults are likewise per
        # store, and travel in each store's URI.
        #
        # `nix_path` leaves for the same reason, and it is the clearest case:
        # `_init_nix` below never reads it. It reaches `open_eval_state`, once
        # for each evaluator, which is why the same session may already give
        # two evaluators two different search paths. Keeping it here made
        # `Session(nix_path=...)` refuse what `NixSettings(nix_path=...)`
        # allows -- two spellings of one per-evaluator setting, disagreeing.
        return (
            self._nix_conf,
            self._load_config,
            tuple(sorted(self._global_settings.to_worker_settings().items())),
            self._verbosity,
        )

    def _init_nix(self) -> None:
        self._provenance = self._runtime.initialize(
            settings=self._global_settings.to_worker_settings(),
            load_config=self._load_config,
            verbosity=None if self._verbosity is None else int(self._verbosity),
        )
        register_import_path_primops(self._primops)

    async def close(  # noqa: C901, PLR0912 -- tracked complexity/arg-count debt, see TODO.md
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,  # noqa: ASYNC109 -- timeout passed to executor.drain → asyncio.wait which accepts a timeout parameter
        force: bool = False,
    ) -> None:
        if not self._opened:
            return
        executor = self._executor
        if executor is None:
            raise RuntimeError("open inproc Session has no Nix executor")
        if not wait and (executor.has_pending_work() or any(eval.has_pending_work() for eval in self._evals)):
            raise RuntimeError("cannot close inproc Session while Nix work is outstanding")
        executor.begin_close(force=force)
        for eval_session in tuple(self._evals):
            eval_session._begin_close(force=force)  # type: ignore[reportPrivateUsage] -- Session owns evaluator executors  # noqa: SLF001
        try:
            await executor.drain(timeout=timeout)
            for eval_session in tuple(self._evals):
                await eval_session._drain(timeout=timeout)  # type: ignore[reportPrivateUsage] -- Session owns evaluator executors  # noqa: SLF001
        except TimeoutError:
            executor.resume()
            for eval_session in tuple(self._evals):
                eval_session._resume()  # type: ignore[reportPrivateUsage] -- Session owns evaluator executors  # noqa: SLF001
            raise

        errors: list[Exception] = []

        async def close_resource(operation: Any) -> None:
            try:
                await operation
            except Exception as exc:
                errors.append(exc)

        try:
            for eval_session in tuple(self._evals):
                await close_resource(eval_session.close())
            for store in tuple(self._stores):
                await close_resource(store.close())
            task = self._log_task
            self._log_task = None
            if task is not None:
                await close_resource(self._collector.aclose())
                await close_resource(task)
            await close_resource(executor.run_closing(nanopynix_util.remove_logger))
        finally:
            try:
                executor.shutdown(wait=True)
            except Exception as exc:
                errors.append(exc)
            self._executor = None
            self._opened = False
            self._restore_nix_conf_env()
            _process_guard.release(self)

        # Cancellation is not collected, and neither is it collected in
        # `close_resource` above. Collecting it did two things, both wrong. The
        # scope that owns the cancellation never saw it, so an enclosing
        # `fail_after` could not turn its own expiry into a `TimeoutError`. And
        # the loop above did not stop, so every resource left was closed under
        # the same cancelled scope and cut short at its first checkpoint --
        # measured as five `CancelledError` for one cancellation, reported as a
        # group. rpc catches `Exception` here for the same reason; the comment
        # in `rpc/client/session.py` records that this file taught it that.
        #
        # Letting it through costs nothing: the `finally` above shuts the
        # executor down and releases the process guard either way.
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("errors closing inproc Session", errors)

    async def _forward_logs(self) -> None:
        async for raw in self._collector.stream():
            kind, request_id, *payload = raw
            if kind == LogStreamEventKind.NIX:
                action, *args = payload
                event = LogEvent(request_id=request_id, action=action, args=args)
            elif kind == LogStreamEventKind.FINALIZED:
                event = LogEvent(request_id=request_id, request_finalized=RequestFinalized())
            else:
                continue
            self._log_bus.emit(event)

    def _check_open(self) -> None:
        if not self._opened:
            raise SessionClosedError("inproc Session is not open — use async with")

    async def run[T](self, func: Callable[..., T], *args: object) -> T:
        """Run one Store-only Nix operation on this session's Store pool."""
        self._check_open()
        executor = self._executor
        if executor is None:
            raise RuntimeError("open inproc Session has no Nix executor")
        operation_id = self._next_operation_id()
        try:
            return await executor.run(_run_with_log_context, operation_id, func, args)
        finally:
            self._collector.request_finalized(operation_id)

    async def _run_closing[T](self, func: Callable[..., T], *args: object) -> T:
        executor = self._executor
        if executor is None:
            raise RuntimeError("open inproc Session has no Nix executor")
        operation_id = self._next_operation_id()
        try:
            return await executor.run_closing(_run_with_log_context, operation_id, func, args)
        finally:
            self._collector.request_finalized(operation_id)

    def _next_operation_id(self) -> int:
        """Allocate the next operation id and tag it into any active capture.

        The tagging lives here rather than at each dispatch site because this
        is the single allocation point: ``Session.run``/``_run_closing`` and
        ``EvalSession.run``/``_run_closing`` all come through it, and an
        evaluator operation that went untagged would leave
        ``LogCapture.wait()`` returning before the work it was capturing had
        finalized. rpc's one chokepoint is ``WorkerClient.invoke()``; this is
        inproc's.
        """
        operation_id = next(self._operation_ids)
        for capture in ACTIVE_LOG_CAPTURES.get():
            capture._register_request(operation_id)  # type: ignore[reportPrivateUsage] -- the ACTIVE_LOG_CAPTURES dispatch contract; rpc does the same  # noqa: SLF001
        return operation_id

    def store(self, uri: StoreConfig | str | None = None) -> Store:
        """Return a direct-pointer store context manager.

        Args:
            uri: A :mod:`nanopynix.stores` model, a store URI, or ``None`` for
                this session's default store. A model has this session's store
                defaults merged under it, so a value set on the model wins.
        """
        selected = self._store_uri if uri is None else resolve_store_spec(uri, self._store_defaults)
        store = Store(self, selected)
        self._stores.add(store)
        return store

    def eval(
        self,
        store: Store,
        *,
        build_store: Store | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> EvalSession:
        """Return a pointer-backed evaluator with its own dedicated Nix thread.

        ``eval_settings``/``fetch_settings`` configure this evaluator alone —
        each :class:`EvalSession` owns an independent Nix evaluator, so
        concurrently open sessions may use different settings. Each is merged
        over this session's own eval and fetch defaults, so a field named here
        wins and the rest come from ``Session(settings=...)``.
        """
        self._require_own_stores(store, build_store)
        return EvalSession(
            self,
            store,
            build_store,
            eval_settings=merge_defaults(eval_settings, self._eval_defaults),
            fetch_settings=merge_defaults(fetch_settings, self._fetch_defaults),
            flake_defaults=self._flake_defaults,
        )

    def repl(
        self,
        store: Store,
        *,
        build_store: Store | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
        line_editors: Sequence[str] = DEFAULT_LINE_EDITORS,
    ) -> ReplSession:
        """Return an evaluator that keeps a persistent Nix REPL scope.

        A :class:`ReplSession` is an :class:`EvalSession`, so it accepts the
        same settings and owns its own dedicated Nix thread. ``line_editors``
        is per-session rather than per-:class:`Session` because it describes
        one interactive front-end, and nothing else in this class consumes it.
        """
        self._require_own_stores(store, build_store)
        return ReplSession(
            self,
            store,
            build_store,
            eval_settings=merge_defaults(eval_settings, self._eval_defaults),
            fetch_settings=merge_defaults(fetch_settings, self._fetch_defaults),
            flake_defaults=self._flake_defaults,
            line_editors=line_editors,
        )

    def _require_own_stores(self, store: Store, build_store: Store | None) -> None:
        """Reject stores opened by a different Session before an evaluator binds them."""
        if store._session is not self:  # type: ignore[reportPrivateUsage] -- identity ownership boundary  # noqa: SLF001
            raise ValueError("Store belongs to a different inproc Session")
        if build_store is not None and build_store._session is not self:  # type: ignore[reportPrivateUsage] -- identity ownership boundary  # noqa: SLF001
            raise ValueError("build_store belongs to a different inproc Session")

    async def get_verbosity(self) -> LogLevel:
        """Return the current Nix log verbosity.

        ``LogLevel`` rather than the bare ``int`` this used to return, matching
        rpc. It is an ``IntEnum``, so this is a widening and not a break --
        comparisons, arithmetic and ``int()`` all behave as before, and
        ``.name`` starts working. Callers written against rpc used ``.name``
        already; ``pynix``'s ``:verbosity`` command is one, and it would have
        raised ``AttributeError`` the moment it was pointed at inproc.
        """
        return LogLevel(await self.run(self._runtime.get_verbosity))

    async def set_verbosity(self, verbosity: LogLevelInput) -> LogLevel:
        """Set the Nix log verbosity and return the resulting level."""
        level = normalize_log_level(verbosity)
        return LogLevel(await self.run(self._runtime.set_verbosity, int(level)))

    async def settings(self, *, overridden_only: bool = False) -> dict[str, str]:
        """Return this session's effective Nix settings.

        Args:
            overridden_only: Report only the settings something set, rather
                than the whole registry. This is what tells an applied value
                apart from a default.

        Returns:
            The settings, as Nix reports them.
        """
        return dict(await self.run(self._runtime.list_settings, overridden_only))

    async def settings_provenance(self) -> SettingsProvenance:
        """Return where this session's configuration came from.

        With ``load_config=True`` the host's ``nix.conf`` and environment are
        read before this session applies its own settings, so the two can be
        told apart. :attr:`SettingsProvenance.overridden_from_config` is the
        interesting part: the settings where the host asked for one value and
        this session uses another.
        """
        return self._provenance

    async def set_settings(self, settings: NixGlobalSettings) -> dict[str, str]:
        """Apply global Nix settings to this session, and read each one back.

        The values come back from Nix rather than being echoed, so a caller
        sees what Nix made of each one. An unknown setting name raises.

        Refuses while a store or an evaluator is open. Nix builds both from the
        globals as they stand and never looks again, so a write now would not
        reach what the caller is holding. Close them, write, and open again.

        Raises:
            SettingNotLiveError: A store or an evaluator is open.
        """
        reject_settings_write_while_open(
            open_stores=sum(1 for store in self._stores if store.is_open),
            open_evaluators=len(self._evals),
        )
        applied = dict(await self.run(self._runtime.apply_settings, settings.to_worker_settings(explicit_only=True)))
        self._provenance = self._provenance.model_copy(
            update={"applied": {**self._provenance.applied, **applied}},
        )
        return applied

    async def log_stream(self) -> AsyncIterator[LogEvent]:
        """Async iterator over log events from this process's Nix logger."""
        send_stream, receive_stream = anyio.create_memory_object_stream[LogEvent](max_buffer_size=math.inf)
        subscription = self.subscribe(send_stream.send_nowait)
        try:
            async for event in receive_stream:
                yield event
        finally:
            subscription.unsubscribe()

    def subscribe(self, callback: Any) -> BusSubscription:
        """Subscribe a callback to live log events. Call ``.unsubscribe()`` to stop."""
        return self._log_bus.subscribe(callback)

    def capture_logs(self) -> LogCapture:
        """Record typed log events during an async context block.

        The same :class:`~nanopynix.logging.LogCapture` rpc returns, over the
        same bus this session's :meth:`subscribe` uses. It was rpc-only, which
        the signature ledger carried as "Session.capture_logs:rpc-only" --
        nothing about capturing log events depends on where Nix runs, and
        inproc already had every part it needs: the bus, the operation ids,
        and the ``request_finalized`` events that let ``wait()`` know when a
        capture is complete.
        """
        return LogCapture(self)


class Store:
    """Async façade over one direct ``nanopynix_store.Store`` pointer."""

    def __init__(self, session: Session, uri: str) -> None:
        self._session = session
        self._uri = uri
        self._core: CoreStore | None = None

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Open the underlying store."""
        if self._core is None:
            self._core = await self._session.run(self._session._runtime.open_store, self._uri)  # type: ignore[reportPrivateUsage] -- session owns local runtime  # noqa: SLF001

    async def close(self, *, force: bool = False) -> None:
        """Close the underlying store, optionally closing its evaluator first."""
        evals = tuple(eval_session for eval_session in self._session._evals if eval_session._store is self)  # type: ignore[reportPrivateUsage] -- Session owns evaluator lifetime tracking  # noqa: SLF001
        if evals:
            if not force:
                raise RuntimeError("cannot close a store while its EvalState is open; close the EvalSession first")
            for eval_session in evals:
                await eval_session.close()
        local = self._core
        self._core = None
        self._session._stores.discard(self)  # type: ignore[reportPrivateUsage] -- session owns store lifetime tracking  # noqa: SLF001
        if local is not None:
            await self._session._run_closing(local.close)  # type: ignore[reportPrivateUsage] -- Store teardown follows Session close ordering  # noqa: SLF001

    def _require_raw(self) -> nanopynix_store.Store:
        if self._core is None:
            raise StoreClosedError("Store is not open — use async with")
        return self._core.require_raw()

    @property
    def is_open(self) -> bool:
        """True while this store is open.

        Read by ``Session.set_settings``: a store reads its settings while Nix
        constructs it, so changing a global while one is open would be lost on
        that store without a word.
        """
        return self._core is not None

    def _require_core(self) -> CoreStore:
        if self._core is None:
            raise StoreClosedError("Store is not open — use async with")
        return self._core

    async def uri(self, *, with_params: bool = False) -> str:
        """Return this store's URI, optionally including configuration parameters."""
        return await self._session.run(
            functools.partial(self._require_core().get_uri, with_params=with_params),
        )

    async def store_dir(self) -> str:
        """Return this store's logical store directory."""
        return await self._session.run(self._require_core().get_store_dir)

    async def parse_store_path(self, path: str) -> StorePath:
        """Validate and normalise ``path`` as a Nix store path."""
        return await self._session.run(self._require_core().parse_store_path, path)

    async def is_valid_path(self, path: str | StorePath) -> bool:
        """Return whether ``path`` is valid in this store."""
        return await self._session.run(self._require_core().is_valid_path, str(path))

    async def query_path_info(self, path: str | StorePath) -> PathInfo:
        """Return metadata for a valid store path."""
        return await self._session.run(self._require_core().query_path_info, str(path))

    async def query_all_valid_paths(self) -> list[StorePath]:
        """Return every valid path registered in this store."""
        return await self._session.run(self._require_core().query_all_valid_paths)

    async def compute_fs_closure(
        self,
        path: str | StorePath,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        """Return the filesystem closure of ``path``."""
        return await self._session.run(
            functools.partial(
                self._require_core().compute_fs_closure,
                str(path),
                flip_direction=flip_direction,
                include_outputs=include_outputs,
                include_derivers=include_derivers,
            ),
        )

    async def query_derivation_outputs(self, path: str | StorePath) -> list[StorePath]:
        """Return output paths declared by a derivation."""
        return await self._session.run(self._require_core().query_derivation_outputs, str(path))

    async def query_valid_derivers(self, path: str | StorePath) -> list[StorePath]:
        """Return valid derivations that produced ``path``."""
        return await self._session.run(self._require_core().query_valid_derivers, str(path))

    async def query_referrers(self, path: str | StorePath) -> list[StorePath]:
        """Return valid store paths that reference ``path``."""
        return await self._session.run(self._require_core().query_referrers, str(path))

    async def follow_links_to_store_path(self, path: str) -> StorePath:
        """Resolve a path that may traverse symlinks to its containing store path."""
        return await self._session.run(self._require_core().follow_links_to_store_path, path)

    async def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        """Return the valid store path whose hash component is ``hash_part``, if any."""
        return await self._session.run(self._require_core().query_path_from_hash_part, hash_part)

    async def query_substitutable_paths(self, paths: list[str | StorePath]) -> list[StorePath]:
        """Return the subset of ``paths`` that can be substituted from a binary cache."""
        return await self._session.run(
            self._require_core().query_substitutable_paths,
            [str(path) for path in paths],
        )

    async def get_build_log(self, path: str | StorePath) -> str | None:
        """Return the build log for ``path``, or ``None`` if no log is available."""
        return await self._session.run(self._require_core().get_build_log, str(path))

    async def query_missing(
        self,
        derived_paths: list[str | StorePath],
        /,
    ) -> MissingInfo:
        """Return which of ``derived_paths`` still need to be built or substituted.

        A plain derivation path means all outputs. A ``^`` separator opts into
        Nix's explicit canonical DerivedPath output-selection syntax.
        """
        return await self._session.run(
            self._require_core().query_missing,
            [str(p) for p in derived_paths],
        )

    async def build_paths_with_results(
        self,
        derived_paths: Sequence[str | StorePath],
        /,
        *,
        build_mode: BuildMode | int | None = None,
        eval_store: Store | None = None,
    ) -> list[BuildResult]:
        """Build derived paths and return one result per Nix build outcome.

        A plain derivation path means all outputs. A ``^`` separator opts into
        Nix's explicit canonical DerivedPath output-selection syntax.
        """
        if eval_store is not None and eval_store._session is not self._session:  # type: ignore[reportPrivateUsage] -- session ownership guard  # noqa: SLF001
            raise ValueError("eval_store belongs to a different inproc Session")
        return await self._session.run(
            functools.partial(
                self._require_core().build_paths_with_results,
                [str(path) for path in derived_paths],
                build_mode=build_mode_value(build_mode),
                eval_store=None if eval_store is None else eval_store._require_core(),  # noqa: SLF001 -- same-session sibling Store, guarded above
            ),
        )

    async def read_derivation(
        self,
        drv_path: str | StorePath,
        /,
    ) -> Derivation:
        """Parse and return the ``.drv`` file at ``drv_path``."""
        return await self._session.run(self._require_core().read_derivation, str(drv_path))

    @no_runtime_type_check  # action validates its own membership in the shared
    # CoreStore.collect_garbage's _RAW_GC_ACTIONS at runtime for untyped
    # callers; beartype's parameter check would otherwise intercept before
    # that guard runs and raise its own exception type instead of ValueError.
    async def collect_garbage(
        self,
        action: GcAction,
        /,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: list[str | StorePath] | tuple[()] = (),
        max_freed: int = NO_GC_LIMIT,
    ) -> GcResult:
        """Run a garbage-collection pass; see :meth:`nanopynix.store.Store.collect_garbage`."""
        return await self._session.run(
            functools.partial(
                self._require_core().collect_garbage,
                action,
                ignore_liveness=ignore_liveness,
                paths_to_delete=[str(p) for p in paths_to_delete],
                max_freed=max_freed,
            ),
        )

    async def add_temp_root(self, path: str | StorePath, /) -> None:
        """Keep ``path`` alive against the garbage collector for this session.

        The root lasts as long as this process holds the store open; it is not
        written to disk as a symlink. Use :meth:`add_perm_root` for a root that
        outlives the process.
        """
        await self._session.run(self._require_core().add_temp_root, str(path))

    async def add_perm_root(self, path: str | StorePath, gc_root: str, /) -> str:
        """Create the symlink ``gc_root`` -> ``path`` and return its resolved path.

        Nix's ``IndirectRootStore::addPermRoot`` registers the indirect root
        itself, so this one call is all an application needs to keep a build
        result alive across process restarts. Requires a local filesystem
        store; a store that has no local root directory raises.
        """
        return await self._session.run(self._require_core().add_perm_root, str(path), gc_root)

    async def add_indirect_root(self, path: str, /) -> None:
        """Register ``path``, an existing user-facing symlink, as an indirect root.

        This is the weak half of :meth:`add_perm_root`, which already does it
        for the symlink it creates. Call this directly only for a symlink you
        made yourself.
        """
        await self._session.run(self._require_core().add_indirect_root, path)

    async def find_roots(self, *, censor: bool = False) -> list[GcRoot]:
        """Return the garbage collector's roots."""
        return await self._session.run(functools.partial(self._require_core().find_roots, censor=censor))

    async def compute_store_path(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Compute the store path content-addressing ``path`` without adding it.

        Pure computation -- it reads ``path`` but writes nothing, so it works
        even against a store that cannot accept an add.
        """
        return await self._session.run(
            functools.partial(
                self._require_core().compute_store_path,
                path,
                name=name,
                method=method,
                hash_algo=hash_algo,
            ),
        )

    async def add_to_store(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Add a file or directory to this store, returning its store path.

        The result is the path :meth:`compute_store_path` predicts for the same
        arguments. ``name`` defaults to the source's filename.
        """
        return await self._session.run(
            functools.partial(
                self._require_core().add_to_store,
                path,
                name=name,
                method=method,
                hash_algo=hash_algo,
            ),
        )

    async def store_dirs(self) -> StoreDirs:
        """Return this store's full set of configured directories.

        Only ``store_dir`` and ``uri`` are always populated: the rest describe
        a store with a local filesystem layout, and Nix leaves them unset for
        stores that have none.
        """
        return await self._session.run(self._require_core().get_store_dirs)

    async def ensure_path(self, path: str | StorePath, /) -> None:
        """Make ``path`` valid in this store, substituting it if necessary.

        A no-op when the path is already valid. When it is not and no
        substituter can supply it, this raises rather than returning quietly.
        """
        await self._session.run(self._require_core().ensure_path, str(path))

    async def copy_closure(
        self,
        paths: list[str | StorePath],
        /,
        dest_store: Store,
        *,
        repair: bool = False,
        check_sigs: bool = True,
        substitute: bool = False,
    ) -> None:
        """Copy the closure of ``paths`` from this store to ``dest_store``.

        ``dest_store`` must come from the same session: both stores are driven
        from that session's single Nix thread, and a store from another session
        belongs to a different one.
        """
        if dest_store._session is not self._session:
            raise ValueError("dest_store belongs to a different Session")
        await self._session.run(
            functools.partial(
                self._require_core().copy_closure,
                [str(path) for path in paths],
                dest_store._require_core(),
                repair=repair,
                check_sigs=check_sigs,
                substitute=substitute,
            ),
        )

    async def optimise_store(self) -> None:
        """Reclaim disk space by hard-linking identical files in this store."""
        await self._session.run(self._require_core().optimise_store)

    async def verify_store(self, *, check_contents: bool = False, repair: bool = False) -> bool:
        """Check this store's consistency, returning whether errors were found.

        ``True`` means Nix found something wrong -- with ``repair`` it also
        means it tried to fix it, not that it succeeded.
        """
        return await self._session.run(
            functools.partial(self._require_core().verify_store, check_contents=check_contents, repair=repair),
        )

    async def _public_store_paths(self, raw_paths: Sequence[nanopynix_store.StorePath | str]) -> list[StorePath]:
        paths = await self._session.run(_print_store_paths, self._require_raw(), raw_paths)
        return [StorePath(path) for path in paths]

    async def call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        """Call an L1 store method on the session's Nix thread.

        This intentionally keeps the complete L1 store surface available while
        dedicated ergonomic methods are added above it.
        """
        raw = self._require_raw()
        target = getattr(raw, method)
        return await self._session.run(_call, target, args, kwargs)


class EvalSession:
    """Own one thread-confined direct ``EvalState`` pointer."""

    def __init__(  # noqa: PLR0913 -- two stores and one settings object for each of Nix's three evaluator-facing scopes; grouping them would hide which scope a field belongs to, which is the whole point of the split
        self,
        session: Session,
        store: Store,
        build_store: Store | None = None,
        *,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
        flake_defaults: NixFlakeSettings | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._build_store = build_store
        self._eval_settings = eval_settings
        self._fetch_settings = fetch_settings
        # Not `_flake_settings`: a flake setting belongs to one flake
        # operation, not to the evaluator, so this is only what those
        # operations fall back to. `Session.eval` passes the session's.
        self._flake_defaults = flake_defaults if flake_defaults is not None else NixFlakeSettings()
        self._core: CoreEvalState | None = None
        self._active = False
        self._locked_flakes: set[LockedFlake] = set()
        self._executor = NixThreadExecutor(
            thread_name_prefix="nix-eval",
            thread_initializer=nanopynix_expr._enter_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook  # noqa: SLF001
            thread_finalizer=nanopynix_expr._exit_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook  # noqa: SLF001
            stack_size=NIX_EVALUATOR_STACK_SIZE,
        )

    async def __aenter__(self) -> EvalSession:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Create this evaluator's ``EvalState`` on its dedicated Nix thread."""
        if self._active:
            return
        if self._executor.closed:
            self._executor = NixThreadExecutor(
                thread_name_prefix="nix-eval",
                thread_initializer=nanopynix_expr._enter_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook  # noqa: SLF001
                thread_finalizer=nanopynix_expr._exit_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook  # noqa: SLF001
                stack_size=NIX_EVALUATOR_STACK_SIZE,
            )
        nix_path = (
            self._eval_settings.nix_path
            if self._eval_settings is not None and self._eval_settings.nix_path is not None
            else self._session._nix_path  # type: ignore[reportPrivateUsage] -- Session owns evaluator configuration  # noqa: SLF001
        )
        rendered_eval = self._eval_settings.to_worker_settings() if self._eval_settings is not None else {}
        rendered_eval.pop(NIX_PATH_SETTING_KEY, None)  # applied via nix_path/searchPath, not the generic settings map
        rendered_fetch = self._fetch_settings.to_worker_settings() if self._fetch_settings is not None else {}
        try:
            self._core = await self.run(
                self._session._runtime.open_eval_state,  # type: ignore[reportPrivateUsage] -- Session owns local runtime  # noqa: SLF001
                self._store._require_core(),  # type: ignore[reportPrivateUsage] -- cross-class Store→EvalSession coupling  # noqa: SLF001
                nix_path,
                None if self._build_store is None else self._build_store._require_core(),  # type: ignore[reportPrivateUsage] -- cross-class Store→EvalSession coupling  # noqa: SLF001
                rendered_eval,
                rendered_fetch,
            )
        except BaseException:
            # By this point the executor's dedicated thread has already run
            # its thread_initializer (GC_register_my_thread) as a side effect
            # of submitting the open_eval_state call above. Without this
            # shutdown, a failure here would abandon that thread still
            # registered with Boehm GC -- it would eventually be torn down by
            # Python's own ThreadPoolExecutor atexit/weakref machinery, which
            # has no knowledge of our thread_finalizer, leaving a
            # GC-registered-but-dead thread that a later, unrelated
            # collection cycle can crash on (pthread_kill on a dead tid).
            self._executor.shutdown(wait=True)
            raise
        self._session._evals.add(self)  # type: ignore[reportPrivateUsage] -- Session owns evaluator lifetime tracking  # noqa: SLF001
        self._active = True

    async def configure(
        self,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> None:
        """Apply live eval and fetch settings to this already-open evaluator.

        Only the settings Nix reads fresh at the point of use can be changed
        this way, such as ``max_call_depth``, ``allowed_uris`` and the lint
        settings. Nix reads the others once, while it builds the evaluator, so
        this raises for those rather than accepting them and doing nothing.
        Every fetch setting is live.

        Raises:
            SettingNotLiveError: A setting Nix reads only at construction, such
                as ``nix_path``, ``pure_eval`` or ``restrict_eval``. Open a new
                evaluator with it: ``session.eval(store, eval_settings=...)``.
        """
        rendered_eval = eval_settings.to_worker_settings() if eval_settings is not None else {}
        rendered_fetch = fetch_settings.to_worker_settings() if fetch_settings is not None else {}
        reject_construction_time_keys(rendered_eval, model=NixEvalSettings, target="evaluator")
        reject_construction_time_keys(rendered_fetch, model=NixFetchSettings, target="evaluator")
        await self.run(self._require_core().configure, rendered_eval, rendered_fetch)

    async def close(self) -> None:
        """Release all values and locked flakes, then destroy this evaluator."""
        if not self._active:
            return
        for locked_flake in tuple(self._locked_flakes):
            await locked_flake.release()
        local = self._core
        self._core = None
        self._active = False
        self._session._evals.discard(self)  # type: ignore[reportPrivateUsage] -- Session owns evaluator lifetime tracking  # noqa: SLF001
        try:
            if local is not None:
                await self._run_closing(local.close)
        finally:
            # Must run even if local.close() raised -- otherwise the
            # executor's thread stays GC-registered but is never handed to
            # our thread_finalizer, only to Python's own ThreadPoolExecutor
            # teardown machinery, which skips it (see open()'s matching note).
            self._executor.shutdown(wait=True)

    def _begin_close(self, *, force: bool) -> None:
        self._executor.begin_close(force=force)

    async def _drain(self, *, timeout: float | None) -> None:  # noqa: ASYNC109 -- timeout passed to executor.drain → asyncio.wait which accepts a timeout parameter
        await self._executor.drain(timeout=timeout)

    def _resume(self) -> None:
        self._executor.resume()

    def has_pending_work(self) -> bool:
        return self._executor.has_pending_work()

    async def run[T](self, func: Callable[..., T], *args: object) -> T:
        """Run evaluator and Value work on this evaluator's dedicated thread."""
        operation_id = self._session._next_operation_id()  # type: ignore[reportPrivateUsage] -- Session owns operation correlation  # noqa: SLF001
        try:
            return await self._executor.run(_run_with_log_context, operation_id, func, args)
        finally:
            self._session._collector.request_finalized(operation_id)  # type: ignore[reportPrivateUsage] -- Session owns the log collector  # noqa: SLF001

    async def _run_closing[T](self, func: Callable[..., T], *args: object) -> T:
        operation_id = self._session._next_operation_id()  # type: ignore[reportPrivateUsage] -- Session owns operation correlation  # noqa: SLF001
        try:
            return await self._executor.run_closing(_run_with_log_context, operation_id, func, args)
        finally:
            self._session._collector.request_finalized(operation_id)  # type: ignore[reportPrivateUsage] -- Session owns the log collector  # noqa: SLF001

    def _require_raw(self) -> nanopynix_expr.EvalState:
        if not self._active or self._core is None:
            raise EvalSessionClosedError("EvalSession is not open — use async with")
        return self._core.require_raw()

    def _require_core(self) -> CoreEvalState:
        if not self._active or self._core is None:
            raise EvalSessionClosedError("EvalSession is not open — use async with")
        return self._core

    async def get_verbosity(self) -> LogLevel:
        """Return the current Nix log verbosity.

        Verbosity is process-wide, not per-evaluator — this reads the same
        setting :meth:`Session.get_verbosity` does, and reaching it from here
        is a convenience for code that holds an evaluator rather than the
        session that made it. A REPL is the motivating case: ``pynix``'s
        ``:verbosity`` command has only its :class:`ReplSession` to hand.

        rpc's ``EvalSession`` has always had this pair; inproc's not having it
        is what ``test_engine_parity``'s "EvalSession.get_verbosity:rpc-only"
        recorded, and nothing about process isolation forced the split.
        """
        self._require_core()
        return await self._session.get_verbosity()

    async def set_verbosity(self, verbosity: LogLevelInput) -> LogLevel:
        """Set the Nix log verbosity and return the resulting level.

        Process-wide, as :meth:`get_verbosity` — this is not scoped to the
        evaluator it is called on.
        """
        self._require_core()
        return await self._session.set_verbosity(verbosity)

    async def string(self, expr: str, path: str = "<string>") -> Value:
        """Evaluate the Nix expression ``expr``.

        Args:
            expr: Nix source to evaluate.
            path: Source name attributed to ``expr`` in error messages.
        """
        local = await self.run(self._require_core().eval_string, expr, path)
        return self._track_value(local)

    async def file(self, path: str) -> Value:
        """Evaluate the Nix expression in the file at ``path``."""
        local = await self.run(self._require_core().eval_file, path)
        return self._track_value(local)

    def _track_value(self, local: CoreValue) -> Value:
        return Value(self, local)

    async def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
        flake_settings: NixFlakeSettings | None = None,
    ) -> LockedFlake:
        """Lock a flake, optionally retaining the lock only in memory.

        ``flake_settings`` is merged over this session's flake defaults, so a
        field named here wins and the rest come from ``Session(settings=...)``.
        """
        local = await self.run(
            partial(
                self._require_core().lock_flake,
                ref,
                update_inputs=update_inputs,
                write_lock_file=write_lock_file,
                flake_settings=merge_defaults(flake_settings, self._flake_defaults).to_worker_settings(),
            ),
        )
        proto = await self.run(_locked_flake_proto, local.require_raw())
        locked_flake = LockedFlake(self, local, proto.description, proto.inputs)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    async def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool = True,
        flake_settings: NixFlakeSettings | None = None,
    ) -> Value:
        """Lock and evaluate a flake in one step.

        ``flake_settings`` is merged over this session's flake defaults, as in
        :meth:`lock_flake`.
        """
        local = await self.run(
            partial(
                self._require_core().eval_flake,
                ref,
                write_lock_file=write_lock_file,
                flake_settings=merge_defaults(flake_settings, self._flake_defaults).to_worker_settings(),
            ),
        )
        return self._track_value(local)

    async def get_flake(self, ref: str) -> FlakeRef:
        """Parse and resolve a flake reference without evaluating its outputs."""
        return await self.run(self._require_core().get_flake, ref)

    async def reset_file_cache(self) -> None:
        """Discard parsed file cache entries before re-evaluating source files."""
        await self.run(self._require_raw().reset_file_cache)


class ReplSession(EvalSession):
    """An :class:`EvalSession` with a persistent Nix lexical scope.

    Bindings submitted with :meth:`line` are available to later calls to
    :meth:`string` and :meth:`file` until this session closes.

    Subclassing is what makes that sentence true. A REPL is an interactive
    evaluator, so entering ``x = 1`` is only useful if the same object can
    then evaluate ``x + 1``; a REPL that can accept bindings but not evaluate
    against them is not a REPL. This mirrors rpc's ``ReplSession``, which has
    always been shaped this way -- the two engines previously disagreed about
    what a repl session *is*, and this is the side that matches the CLI it
    exists to serve.
    """

    def __init__(  # noqa: PLR0913 -- EvalSession's six parameters plus line_editors; narrowing either would drop a capability
        self,
        session: Session,
        store: Store,
        build_store: Store | None = None,
        *,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
        flake_defaults: NixFlakeSettings | None = None,
        line_editors: Sequence[str] = DEFAULT_LINE_EDITORS,
    ) -> None:
        super().__init__(
            session,
            store,
            build_store,
            eval_settings=eval_settings,
            fetch_settings=fetch_settings,
            flake_defaults=flake_defaults,
        )
        self._line_editors = tuple(line_editors)
        self._repl_begun = False

    @property
    def line_editors(self) -> tuple[str, ...]:
        """Editor-name substrings that support Nix's ``+LINE`` argument."""
        return self._line_editors

    async def __aenter__(self) -> ReplSession:
        await self.open()
        return self

    async def open(self) -> None:
        """Open the evaluator, then begin its persistent REPL scope.

        Idempotent, because :meth:`EvalSession.open` is. C++'s ``begin_repl``
        raises on a second call rather than no-opping, so without the flag a
        harmless double ``open()`` would surface an opaque "REPL scope is
        already active" RuntimeError from the bindings.
        """
        await super().open()
        if self._repl_begun:
            return
        try:
            await self.run(self._require_raw().begin_repl)
        except BaseException:
            await self.close()
            raise
        self._repl_begun = True

    async def close(self) -> None:
        """Close the evaluator; a later :meth:`open` begins a fresh REPL scope.

        The REPL env belongs to the ``EvalState``, so closing discards it and
        reopening must call ``begin_repl`` again.
        """
        self._repl_begun = False
        await super().close()

    async def line(self, text: str, path: str = "<string>") -> Value | None:
        """Process one Nix REPL line.

        A binding such as ``x = 1`` returns ``None``. An expression returns a
        session-bound :class:`Value`.
        """
        local = await self.run(self._require_core().repl_process_line, text, path)
        return None if local is None else self._track_value(local)

    async def load_file(self, path: str) -> Value:
        """Load a Nix expression file as ``nix repl :load`` does."""
        local = await self.run(self._require_core().repl_load_file, path)
        return self._track_value(local)

    async def add_attrs(self, value: Value) -> list[str]:
        """Add all attributes from ``value`` to this REPL's lexical scope."""
        local_value = await value._resolve_for(self)  # type: ignore[reportPrivateUsage] -- same-evaluator guard  # noqa: SLF001
        return await self.run(self._require_core().repl_add_attrs, local_value)

    async def scope_names(self) -> list[str]:
        """Return the identifiers visible in this REPL's lexical scope."""
        return await self.run(self._require_raw().repl_scope_names)

    async def string(self, expr: str, path: str = "<string>") -> Value:
        """Evaluate ``expr`` in this REPL's scope rather than the base scope.

        Nix keeps the REPL's bindings in a separate env whose parent is
        ``baseEnv``, and C++ exposes one entrypoint per env -- ``eval_string``
        sees only the base scope, ``repl_eval_string`` sees the REPL's. Without
        this override, ``line("x = 1")`` followed by ``string("x")`` raises
        UndefinedVarError, which is the opposite of what a REPL is for. The
        worker has always dispatched this way (on ``repl_active()``); inproc
        never did, so this was a silent semantic divergence that the parity
        detector cannot see -- both engines have ``string`` with an identical
        signature.
        """
        local = await self.run(self._require_core().repl_eval_string, expr, path)
        return self._track_value(local)

    async def file(self, path: str) -> Value:
        """Evaluate the file at ``path`` in this REPL's scope; see :meth:`string`."""
        local = await self.run(self._require_core().repl_eval_file, path)
        return self._track_value(local)


class LockedFlake:
    """Async façade over one thread-confined in-memory flake lock."""

    def __init__(
        self,
        eval_session: EvalSession,
        local: CoreLockedFlake,
        description: str,
        inputs: dict[str, LockedInput],
    ) -> None:
        self._eval_session = eval_session
        self._core: CoreLockedFlake | None = local
        self.description = description
        self.inputs = inputs

    def _local_for(self) -> CoreLockedFlake:
        self._eval_session._require_core()  # type: ignore[reportPrivateUsage] -- parent owns local evaluator lifetime  # noqa: SLF001
        if self._core is None:
            raise LockedFlakeReleasedError("LockedFlake has been released")
        return self._core

    async def eval(self) -> Value:
        """Evaluate this locked flake's outputs."""
        evaluator = self._eval_session._require_core()  # type: ignore[reportPrivateUsage] -- parent owns the local evaluator  # noqa: SLF001
        local = await self._eval_session.run(
            evaluator.call_locked_flake,
            self._local_for(),
        )
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking  # noqa: SLF001

    async def write_lock_file(self) -> None:
        """Persist this locked flake's lock file to disk."""
        await self._eval_session.run(self._local_for().write_lock_file)

    async def release(self) -> None:
        """Release the underlying handle for this locked flake. Idempotent."""
        local = self._core
        self._core = None
        self._eval_session._locked_flakes.discard(self)  # type: ignore[reportPrivateUsage] -- evaluator owns facade lifetime tracking  # noqa: SLF001
        if local is not None:
            await self._eval_session._run_closing(local.close)  # type: ignore[reportPrivateUsage] -- flake teardown follows evaluator close ordering  # noqa: SLF001


class Value:
    """Async façade over a thread-confined :class:`CoreValue`.

    A value is in one of two states. A *resolved* value holds a rooted
    ``CoreValue``. A *lazy* value holds only a parent and one selector -- an
    attribute name or a list index -- and no Nix work has been done for it at
    all; the selection happens the first time something forces it.

    That is rpc's ``ValueProxy`` shape, adopted here so that navigating a
    value tree reads the same on both engines. It used to be inproc's alone
    that made ``attr``/``list_get`` coroutines, which the signature ledger
    carried as ``Value.attr:async`` and ``Value.list_get:async``: chained
    selection had to be spelled ``(await (await v.attr("a")).attr("b"))`` on
    one engine and ``v.attr("a").attr("b")`` on the other, so a caller could
    not be written once. Deferring also means a branch nobody reads is never
    evaluated, which is the property that makes the rpc spelling worth
    matching rather than the other way round.
    """

    def __init__(
        self,
        eval_session: EvalSession,
        local: CoreValue | None = None,
        *,
        pending: tuple[Value, str | int] | None = None,
    ) -> None:
        self._eval_session = eval_session
        self._core: CoreValue | None = local
        self._pending = pending
        self._released = False

    async def __aenter__(self) -> Value:
        self._check_active()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    async def release(self) -> None:
        """Release this value's rooted L1 object. This operation is idempotent.

        ``release`` and not ``close``: rpc has only ``release``, inproc used to
        have both with ``release`` a one-line alias, and two names for one
        operation on one engine is the kind of thing that makes an API look
        like it has two concepts.

        Releasing a value that was never forced costs no hop onto the Nix
        thread -- there is nothing rooted yet to release -- but it still marks
        the value released, so forcing it afterwards raises rather than
        quietly performing the selection.
        """
        local = self._core
        self._core = None
        self._pending = None
        self._released = True
        if local is not None:
            await self._eval_session.run(local.close)

    def _check_active(self) -> None:
        """Reject use of a released value, without requiring it to be resolved."""
        self._eval_session._require_core()  # type: ignore[reportPrivateUsage] -- liveness check before pointer use  # noqa: SLF001
        if self._released:
            raise ValueReleasedError("Value has been released")

    async def _resolve(self) -> CoreValue:
        """Perform the deferred selection, if any, and return the rooted value.

        Parent-first, one hop per link, mirroring rpc's
        ``ValueProxy._ensure_resolved``. Collapsing a whole chain into a single
        dispatch onto the Nix thread would be faster and is deliberately not
        done: a selection that raises partway would leave the links before it
        rooted with no ``Value`` to release them, and a cross-engine test that
        only compares the final result cannot see that leak.
        """
        self._check_active()
        pending = self._pending
        if pending is not None:
            parent, selector = pending
            parent_local = await parent._resolve()  # noqa: SLF001 -- lazy chains walk their own parents
            select = parent_local.attr_get if isinstance(selector, str) else parent_local.list_get
            self._core = await self._eval_session.run(select, selector)  # type: ignore[arg-type] -- selector type matches the accessor picked above
            self._pending = None
        local = self._core
        if local is None:
            raise ValueReleasedError("Value has been released")
        return local

    async def _resolve_for(self, eval_session: EvalSession) -> CoreValue:
        """Force this value on behalf of ``eval_session``, rejecting a foreign one.

        The async replacement for a synchronous ``_local_for``: a caller can
        now be handed a value that has never touched Nix, so reaching its
        rooted ``CoreValue`` is no longer something that can happen without a
        hop.
        """
        if eval_session is not self._eval_session:
            raise ValueError("Value belongs to a different inproc EvalSession")
        return await self._resolve()

    async def to_python(self, *, copy_to_store: bool = False) -> Any:
        """Convert the whole value tree to plain Python data, in one C++ pass.

        This is Nix's own ``printValueAsJSON`` with ``strict=true``, so the
        rules are Nix's rather than ours: an attrset with ``__toString``
        becomes that string, an attrset with ``outPath`` (a derivation, for
        instance) becomes the output path, and anything Nix will not flatten
        -- a function -- is an error rather than a placeholder.

        Converting *is* lossy, deliberately: that ``outPath`` rule is what
        makes a derivation convertible at all, since ``drv.out`` is ``drv``.
        Contrast the ``as_*`` accessors, which convert nothing and raise when
        the value is not already of the type asked for.

        This replaces ``to_python`` and ``json``, which were two more names
        for the same operation.

        Args:
            copy_to_store: If True, a ``path`` value is copied into the Nix
                store and rendered as a store path -- what string
                interpolation of a path does. If False (default), it renders
                as a literal filesystem path, matching ``nix eval --json``.
        """
        return await self._eval_session.run((await self._resolve()).to_json, copy_to_store)

    async def get_type(self) -> NixType:
        """Evaluate to WHNF and return the resulting Nix type.

        Named and typed to match rpc's. This used to be ``type() -> str``,
        returning a name like ``"string"``, so the two engines disagreed on
        both the spelling and the return type -- porting a caller meant
        changing how the result was compared, not just what it was called.
        Every real consumer was already on the rpc spelling.

        This also absorbed ``force()``, which on this engine was
        ``value.force(); value.to_python()`` -- a deep conversion wearing a
        WHNF verb's name, and so an exact duplicate of :meth:`to_python`.
        To force without converting, call this and ignore the answer.
        """
        name = await self._eval_session.run((await self._resolve()).type_name)
        return NixType.from_string(name)  # type: ignore[attr-defined] -- added in models.py

    async def as_int(self) -> int:
        """Force this value and return it as ``int``. Raises if not an int."""
        return await self._eval_session.run((await self._resolve()).as_int)

    async def as_float(self) -> float:
        """Force this value and return it as ``float``. Raises if not a float."""
        return await self._eval_session.run((await self._resolve()).as_float)

    async def as_bool(self) -> bool:
        """Force this value and return it as ``bool``. Raises if not a bool."""
        return await self._eval_session.run((await self._resolve()).as_bool)

    async def as_string(self) -> str:
        """Force this value and return it as ``str``. Raises if not a string."""
        return await self._eval_session.run((await self._resolve()).as_string)

    async def apply(self, function: str | Value) -> Value:
        """Apply a Nix function to this value and return the result.

        The general form of "do a Nix thing to this value". A ``str`` is any
        Nix function expression -- a builtin, or a lambda::

            await (await value.apply("builtins.toString")).as_string()
            await (await value.apply("builtins.length")).as_int()
            await (await value.apply("x: x * 2")).as_int()

        Passing an already-evaluated function instead of a string hoists the
        evaluation out of a loop, which is the whole of what a memoised
        ``apply`` would buy::

            to_string = await eval_session.string("builtins.toString")
            for value in values:
                await (await value.apply(to_string)).as_string()

        This deliberately replaces a family of dedicated coercion methods.
        ``coerce_str`` was only ``builtins.toString`` spelled a second way, and
        the int/float/bool variants had no Nix counterpart at all.
        """
        if isinstance(function, str):
            function = await self._eval_session.string(function, "<apply>")
        return await function.call(self)

    async def realise_string(self) -> str:
        """Coerce this value to a string and realise its Nix string context."""
        return await self._eval_session.run((await self._resolve()).realise_string)

    async def realise_argv(self) -> list[str]:
        """Coerce a Nix list to argv and realise all of its string contexts."""
        return await self._eval_session.run((await self._resolve()).realise_argv)

    async def edit_location(self) -> tuple[str, int]:
        """Return the physical file path and line Nix would open for this value."""
        location = await self._eval_session.run((await self._resolve()).edit_location)
        return location["path"], location["line"]

    def attr(self, name: str) -> Value:
        """Select attribute ``name``, deferring the Nix work until forced.

        Synchronous and lazy, matching rpc: ``value.attr("a").attr("b")``
        builds a chain without touching Nix, and nothing is evaluated until
        something forces the result. See :class:`Value` for why this shape and
        not the coroutine this used to be.

        Deferring the *work* is not deferring the *liveness check*: selecting
        off a released value, or off one whose session has closed, is refused
        here rather than at the eventual force. rpc's ``ValueProxy.attr`` does
        the same, and it has to -- a chain built on a dead parent can never
        resolve, so reporting it at construction is both earlier and the only
        way the two engines agree on where the error comes from.
        """
        self._check_active()
        return Value(self._eval_session, pending=(self, name))

    async def as_dict(self) -> dict[str, Value]:
        """Force this value as an attrset; keys now, values still lazy.

        This is WHNF for an attrset exactly as Nix models it -- forcing an
        attrset gives you its keys and leaves every value a thunk -- so nothing
        here is converted. That is also why it is ``as_`` and not ``to_``: an
        attrset already *is* a mapping of names to values, and this hands it
        over rather than turning it into something else.

        It is the way to reach a value ``to_python()`` cannot flatten. A Nix
        function has no JSON form, so an attrset containing one -- nanopynix's
        own ``builtins.parseNetwork`` result, for instance -- cannot be
        converted whole by ``to_python()`` on either engine, matching Nix. It
        can be read one level at a time here, with the function entries
        reachable through ``apply()``.

        One dispatch onto the Nix thread rather than one per attribute.
        """
        children = await self._eval_session.run(_attr_values, await self._resolve())
        track = self._eval_session._track_value  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking  # noqa: SLF001
        return {name: track(child) for name, child in children.items()}

    async def as_list(self) -> list[Value]:
        """Force this value as a list; length now, elements still lazy.

        The list half of :meth:`as_dict`, with the same reasoning.
        """
        children = await self._eval_session.run(_list_values, await self._resolve())
        track = self._eval_session._track_value  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking  # noqa: SLF001
        return [track(child) for child in children]

    async def has_attr(self, name: str) -> bool:
        """Force this value as an attrset and return whether ``name`` is present."""
        return await self._eval_session.run((await self._resolve()).has_attr, name)

    def list_get(self, index: int) -> Value:
        """Select element ``index``, deferring the Nix work until forced.

        The list half of :meth:`attr`, with the same reasoning, liveness
        check included.
        """
        self._check_active()
        return Value(self._eval_session, pending=(self, index))

    async def attr_names(self) -> list[str]:
        """Force this value as an attrset and return its attribute names."""
        return await self._eval_session.run((await self._resolve()).attr_names)

    async def list_length(self) -> int:
        """Force this value as a list and return its length."""
        return await self._eval_session.run((await self._resolve()).list_length)

    async def call(self, *args: Value | Any) -> Value:
        """Call this value as a Nix function with ``args``.

        Nix functions are curried, so more than one argument is more than one
        application: ``f(a, b)`` is ``f a b``. This used to take exactly one
        ``argument``, which the signature ledger carried as
        ``Value.call:params`` -- a two-argument call worked on rpc only.

        Args:
            *args: Each argument may be a ``Value`` from the same
                ``EvalSession``, or a plain Python value to convert to a Nix
                value.

        Raises:
            TypeError: No arguments were given; Nix has no nullary
                application.
        """
        local = await self._resolve()
        arguments = [await self._argument_core(argument) for argument in args]
        result = await self._eval_session.run(local.call, *arguments)
        return self._eval_session._track_value(result)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking  # noqa: SLF001

    async def __call__(self, *args: Value | Any) -> Value:
        return await self.call(*args)

    async def auto_call(self) -> Value:
        """Apply Nix top-level auto-call semantics to a function value."""
        result = await self._eval_session.run((await self._resolve()).auto_call)
        return self._eval_session._track_value(result)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking  # noqa: SLF001

    async def _argument_core(self, argument: Value | Any) -> CoreValue:
        if isinstance(argument, Value):
            return await argument._resolve_for(self._eval_session)  # noqa: SLF001 -- same-evaluator guard, and the argument may itself be unforced
        return await self._eval_session.run(self._eval_session._require_core().value_from_python, argument)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→Value coupling  # noqa: SLF001

    async def build(self, *, build_mode: BuildMode | int | None = None, store: Store | None = None) -> dict[str, str]:
        """Build the derivation represented by this evaluated value.

        Args:
            build_mode: A :data:`BuildMode` value, or ``None`` for normal builds.
            store: Store to build into; defaults to the store this value's
                ``EvalSession`` was opened with.
        """
        target_store = self._eval_session._store if store is None else store  # type: ignore[reportPrivateUsage] -- evaluator's bound store is default  # noqa: SLF001
        if target_store._session is not self._eval_session._session:  # type: ignore[reportPrivateUsage] -- session ownership guard  # noqa: SLF001
            raise ValueError("Store belongs to a different inproc Session")
        # The canonical DerivedPath string, which for a plain derivation is
        # its .drvPath and selects all outputs. Taken from L1 directly rather
        # than through a method of its own: build() is the only caller, and a
        # public accessor for the intermediate would be inproc-only surface
        # with nothing on the other engine to match it.
        derived_path = await self._eval_session.run((await self._resolve()).derived_path)
        results = await target_store.build_paths_with_results(
            [derived_path],
            build_mode=build_mode,
            eval_store=None if target_store is self._eval_session._store else self._eval_session._store,  # type: ignore[reportPrivateUsage] -- cross-store build source  # noqa: SLF001
        )
        if not results:
            raise StoreError("StoreError", f"build returned no result for {derived_path}")
        if not results[0].success:
            # Same structured status, same exception type as the rpc engine --
            # see nanopynix.exceptions.build_error_from_result.
            raise build_error_from_result(
                status=results[0].status,
                error_msg=results[0].error_msg,
                drv_path=results[0].drv_path or derived_path,
            )
        derivation = await target_store.read_derivation(derived_path)
        return {name: output.path for name, output in derivation.outputs.items() if output.path is not None}


def _call(target: Any, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    return target(*args, **kwargs)


def _attr_values(local: Any) -> dict[str, Any]:
    """Every attribute of an attrset, in one hop onto the Nix thread."""
    return {name: local.attr_get(name) for name in local.attr_names()}


def _list_values(local: Any) -> list[Any]:
    """Every element of a list, in one hop onto the Nix thread."""
    return [local.list_get(index) for index in range(local.list_length())]


__all__ = [
    "BuildMode",
    "EvalSession",
    "LockedFlake",
    "RawEvalState",
    "RawGCAction",
    "RawStore",
    "RawStorePath",
    "RawValue",
    "ReplSession",
    "Session",
    "Store",
    "Value",
]
