"""Asynchronous in-process Nix API backed by direct L1 object pointers.

Unlike :class:`nanopynix.Session`, this module does not start a worker
process. Store work uses a bounded thread pool, while each evaluator owns a
dedicated thread so callers retain an asynchronous API without exposing Nix's
evaluator thread-affinity requirements.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import store as nanopynix_store
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.store import GcAction as PublicGcAction

from nanopynix._core._extract import locked_flake as _locked_flake_proto
from nanopynix._core._local import LocalEvalState, LocalLockedFlake, LocalRuntime, LocalStore, LocalValue
from nanopynix._core._nix_executor import NixThreadExecutor
from nanopynix.logging import LogCollector
from nanopynix.models import BuildResult, Derivation, GcResult, LockedInput, LogEvent, MissingInfo, PathInfo
from nanopynix.models import StorePath as PublicStorePath
from nanopynix.settings import NixEvalSettings, NixFetchSettings, NixFlakeSettings, NixSettings, normalize_nix_settings
from nanopynix.verbosity import LogLevelInput, normalize_log_level

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from os import PathLike


BuildMode = nanopynix_store.BuildMode
GCAction = nanopynix_store.GCAction
RawEvalState = nanopynix_expr.EvalState
RawStore = nanopynix_store.Store
RawValue = nanopynix_expr.Value
StorePath = nanopynix_store.StorePath


_RAW_GC_ACTIONS = {
    PublicGcAction.RETURN_LIVE: GCAction.ReturnLive,
    PublicGcAction.RETURN_DEAD: GCAction.ReturnDead,
    PublicGcAction.DELETE_DEAD: GCAction.DeleteDead,
    PublicGcAction.DELETE_SPECIFIC: GCAction.DeleteSpecific,
}


def _raw_gc_action(action: PublicGcAction) -> Any:
    try:
        return _RAW_GC_ACTIONS[action]
    except KeyError as exc:
        raise ValueError(f"unsupported garbage-collection action: {action!r}") from exc

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
                raise RuntimeError("Nix is already initialized in this process with different nanopynix.inproc settings")
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


def _print_store_path(raw_store: Any, raw_path: Any) -> str:
    path = str(raw_path)
    store_dir = raw_store.get_store_dir().rstrip("/")
    if path == store_dir or path.startswith(f"{store_dir}/"):
        return path
    return f"{store_dir}/{path}"


def _print_store_paths(raw_store: Any, raw_paths: Any) -> list[str]:
    return [_print_store_path(raw_store, path) for path in raw_paths]


def _parse_store_paths(raw_store: Any, paths: list[str]) -> list[Any]:
    return [raw_store.parse_store_path(path) for path in paths]


def _run_with_log_context(operation_id: int, func: Any, args: tuple[Any, ...]) -> Any:
    previous = nanopynix_util.get_logger_request_id()
    nanopynix_util.set_logger_request_id(operation_id)
    try:
        return func(*args)
    finally:
        nanopynix_util.set_logger_request_id(previous)


class InprocSessionClosedError(RuntimeError):
    """Raised when an in-process session resource is used after close."""


class InprocValueReleasedError(RuntimeError):
    """Raised when an in-process value is used after its explicit release."""


class InprocLockedFlakeReleasedError(RuntimeError):
    """Raised when an in-process locked flake is used after release."""


class _LogSubscription:
    def __init__(self, session: Session, callback: Any) -> None:
        self._session = session
        self._callback = callback

    def unsubscribe(self) -> None:
        self._session.unsubscribe(self._callback)


class Session:
    """Own one asynchronous, pointer-backed in-process Nix runtime.

    Nix library initialization and logger installation are process-global. To
    make that constraint explicit, only one :class:`Session` may be open at a
    time. Store work uses a bounded pool and each live :class:`EvalSession`
    owns one separate Nix thread.
    """

    def __init__(
        self,
        *,
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: NixSettings | PathLike[str] | str | None = None,
        experimental_features: Sequence[str] | None = None,
        verbosity: LogLevelInput | None = None,
        nix_path: str | Sequence[str] | None = None,
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
        self._nix_path = self._normalize_nix_path(nix_path)
        if store_workers < 1:
            raise ValueError("store_workers must be at least 1")
        self._store_workers = store_workers
        self._collector = LogCollector()
        self._runtime = LocalRuntime()
        # Creation is deliberately lazy: merely importing nanopynix.inproc
        # must not start a Nix thread in an L3 manager process.
        self._executor: NixThreadExecutor | None = None
        self._log_callbacks: set[Any] = set()
        self._log_task: asyncio.Task[None] | None = None
        self._opened = False
        self._operation_ids = itertools.count(1)
        self._evals: set[EvalSession] = set()
        self._stores: set[Store] = set()

    @staticmethod
    def _normalize_nix_path(nix_path: str | Sequence[str] | None) -> list[str]:
        if nix_path is None:
            return list(nanopynix_expr.parse_nix_path())
        if isinstance(nix_path, str):
            return list(nanopynix_expr.parse_nix_path(nix_path))
        return list(nix_path)

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
                os.environ["NIX_USER_CONF_FILES"] = str(self._nix_conf)
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
                    _process_guard.release(self)
            raise

    def _initialization_signature(self) -> tuple[object, ...]:
        return (
            self._nix_conf,
            self._load_config,
            tuple(sorted(self._settings.to_worker_settings().items())),
            self._verbosity,
            tuple(self._nix_path),
        )

    def _init_nix(self) -> None:
        self._runtime.initialize(
            settings=self._settings.to_worker_settings(),
            load_config=self._load_config,
            verbosity=None if self._verbosity is None else int(self._verbosity),
        )

    async def close(
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
            eval_session._begin_close(force=force)  # type: ignore[reportPrivateUsage] -- Session owns evaluator executors
        try:
            await executor.drain(timeout=timeout)
            for eval_session in tuple(self._evals):
                await eval_session._drain(timeout=timeout)  # type: ignore[reportPrivateUsage] -- Session owns evaluator executors
        except TimeoutError:
            executor.resume()
            for eval_session in tuple(self._evals):
                eval_session._resume()  # type: ignore[reportPrivateUsage] -- Session owns evaluator executors
            raise

        errors: list[BaseException] = []

        async def close_resource(operation: Any) -> None:
            try:
                await operation
            except BaseException as exc:
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
            except BaseException as exc:
                errors.append(exc)
            self._executor = None
            self._opened = False
            _process_guard.release(self)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("errors closing inproc Session", errors)

    async def _forward_logs(self) -> None:
        async for raw in self._collector.stream():
            kind, request_id, *payload = raw
            if kind != "nix":
                continue
            action, *args = payload
            event = LogEvent(request_id=request_id, action=action, args=args)
            for callback in tuple(self._log_callbacks):
                callback(event)

    def _check_open(self) -> None:
        if not self._opened:
            raise InprocSessionClosedError("inproc Session is not open — use async with")

    async def run(self, func: Any, *args: Any) -> Any:
        """Run one Store-only Nix operation on this session's Store pool."""
        self._check_open()
        executor = self._executor
        if executor is None:
            raise RuntimeError("open inproc Session has no Nix executor")
        operation_id = self._next_operation_id()
        return await executor.run(_run_with_log_context, operation_id, func, args)

    async def _run_closing(self, func: Any, *args: Any) -> Any:
        executor = self._executor
        if executor is None:
            raise RuntimeError("open inproc Session has no Nix executor")
        operation_id = self._next_operation_id()
        return await executor.run_closing(_run_with_log_context, operation_id, func, args)

    def _next_operation_id(self) -> int:
        return next(self._operation_ids)

    def unsubscribe(self, callback: Any) -> None:
        """Remove one callback previously registered with :meth:`subscribe`."""
        self._log_callbacks.discard(callback)

    def store(self, uri: str = "auto") -> Store:
        """Return a direct-pointer store context manager."""
        store = Store(self, uri)
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
        concurrently open sessions may use different settings.
        """
        if store._session is not self:  # type: ignore[reportPrivateUsage] -- identity ownership boundary
            raise ValueError("Store belongs to a different inproc Session")
        if build_store is not None and build_store._session is not self:  # type: ignore[reportPrivateUsage] -- identity ownership boundary
            raise ValueError("build_store belongs to a different inproc Session")
        return EvalSession(self, store, build_store, eval_settings=eval_settings, fetch_settings=fetch_settings)

    async def get_verbosity(self) -> int:
        """Return the current Nix log verbosity."""
        return await self.run(self._runtime.get_verbosity)

    async def set_verbosity(self, verbosity: LogLevelInput) -> int:
        """Set the Nix log verbosity and return the resulting level."""
        level = normalize_log_level(verbosity)
        return await self.run(self._runtime.set_verbosity, int(level))

    async def log_stream(self) -> AsyncIterator[LogEvent]:
        """Async iterator over log events from this process's Nix logger."""
        queue: asyncio.Queue[LogEvent] = asyncio.Queue()
        subscription = self.subscribe(queue.put_nowait)
        try:
            while True:
                yield await queue.get()
        finally:
            subscription.unsubscribe()

    def subscribe(self, callback: Any) -> _LogSubscription:
        """Subscribe a callback to live log events. Call ``.unsubscribe()`` to stop."""
        self._log_callbacks.add(callback)
        return _LogSubscription(self, callback)


class Store:
    """Async façade over one direct ``nanopynix_store.Store`` pointer."""

    def __init__(self, session: Session, uri: str) -> None:
        self._session = session
        self._uri = uri
        self._local: LocalStore | None = None

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Open the underlying store."""
        if self._local is None:
            self._local = await self._session.run(self._session._runtime.open_store, self._uri)  # type: ignore[reportPrivateUsage] -- session owns local runtime

    async def close(self, *, force: bool = False) -> None:
        """Close the underlying store, optionally closing its evaluator first."""
        evals = tuple(eval_session for eval_session in self._session._evals if eval_session._store is self)  # type: ignore[reportPrivateUsage] -- Session owns evaluator lifetime tracking
        if evals:
            if not force:
                raise RuntimeError("cannot close a store while its EvalState is open; close the EvalSession first")
            for eval_session in evals:
                await eval_session.close()
        local = self._local
        self._local = None
        self._session._stores.discard(self)  # type: ignore[reportPrivateUsage] -- session owns store lifetime tracking
        if local is not None:
            await self._session._run_closing(local.close)  # type: ignore[reportPrivateUsage] -- Store teardown follows Session close ordering

    def _require_raw(self) -> Any:
        if self._local is None:
            raise InprocSessionClosedError("Store is not open — use async with")
        return self._local.require_raw()

    def _require_local(self) -> LocalStore:
        if self._local is None:
            raise InprocSessionClosedError("Store is not open — use async with")
        return self._local

    async def uri(self) -> str:
        """Return the canonical URI of this store."""
        return await self._session.run(self._require_raw().get_uri)

    async def store_dir(self) -> str:
        """Return this store's logical store directory."""
        return await self._session.run(self._require_raw().get_store_dir)

    async def parse_store_path(self, path: str) -> PublicStorePath:
        """Validate and normalise ``path`` as a Nix store path."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, path)
        return PublicStorePath(await self._session.run(_print_store_path, self._require_raw(), raw_path))

    async def is_valid_path(self, path: str | PublicStorePath) -> bool:
        """Return whether ``path`` is valid in this store."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        return await self._session.run(self._require_raw().is_valid_path, raw_path)

    async def query_path_info(self, path: str | PublicStorePath) -> PathInfo:
        """Return metadata for a valid store path."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        raw_info = await self._session.run(self._require_raw().query_path_info, raw_path)
        return PathInfo(**raw_info)

    async def query_all_valid_paths(self) -> list[PublicStorePath]:
        """Return every valid path registered in this store."""
        paths = await self._session.run(self._require_raw().query_all_valid_paths)
        return await self._public_store_paths(paths)

    async def compute_fs_closure(
        self,
        path: str | PublicStorePath,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[PublicStorePath]:
        """Return the filesystem closure of ``path``."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        paths = await self._session.run(
            self._require_raw().compute_fs_closure,
            raw_path,
            flip_direction,
            include_outputs,
            include_derivers,
        )
        return await self._public_store_paths(paths)

    async def query_derivation_outputs(self, path: str | PublicStorePath) -> list[PublicStorePath]:
        """Return output paths declared by a derivation."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        paths = await self._session.run(self._require_raw().query_derivation_outputs, raw_path)
        return await self._public_store_paths(paths)

    async def query_valid_derivers(self, path: str | PublicStorePath) -> list[PublicStorePath]:
        """Return valid derivations that produced ``path``."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        paths = await self._session.run(self._require_raw().query_valid_derivers, raw_path)
        return await self._public_store_paths(paths)

    async def query_referrers(self, path: str | PublicStorePath) -> list[PublicStorePath]:
        """Return valid store paths that reference ``path``."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        paths = await self._session.run(self._require_raw().query_referrers, raw_path)
        return await self._public_store_paths(paths)

    async def follow_links_to_store_path(self, path: str) -> PublicStorePath:
        """Resolve a path that may traverse symlinks to its containing store path."""
        raw_path = await self._session.run(self._require_raw().follow_links_to_store_path, path)
        return PublicStorePath(await self._session.run(_print_store_path, self._require_raw(), raw_path))

    async def query_path_from_hash_part(self, hash_part: str) -> PublicStorePath | None:
        """Return the valid store path whose hash component is ``hash_part``, if any."""
        raw_path = await self._session.run(self._require_raw().query_path_from_hash_part, hash_part)
        if raw_path is None:
            return None
        return PublicStorePath(await self._session.run(_print_store_path, self._require_raw(), raw_path))

    async def query_substitutable_paths(self, paths: list[str | PublicStorePath]) -> list[PublicStorePath]:
        """Return the subset of ``paths`` that can be substituted from a binary cache."""
        raw_paths = await self._session.run(_parse_store_paths, self._require_raw(), [str(path) for path in paths])
        result = await self._session.run(self._require_raw().query_substitutable_paths, raw_paths)
        return await self._public_store_paths(result)

    async def get_build_log(self, path: str | PublicStorePath) -> str | None:
        """Return the build log for ``path``, or ``None`` if no log is available."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(path))
        return await self._session.run(self._require_raw().get_build_log, raw_path)

    async def query_missing(
        self,
        derived_paths: list[str | PublicStorePath],
        /,
    ) -> MissingInfo:
        """Return which of ``derived_paths`` still need to be built or substituted."""
        raw_paths = await self._session.run(
            _parse_store_paths, self._require_raw(), [str(p) for p in derived_paths]
        )
        result = await self._session.run(self._require_raw().query_missing, raw_paths)
        return MissingInfo(**result)

    async def build_paths_with_results(
        self,
        derived_paths: Sequence[str | PublicStorePath],
        /,
        *,
        build_mode: Any = None,
        eval_store: Store | None = None,
    ) -> list[BuildResult]:
        """Build derived paths and return one result per Nix build outcome.

        A plain derivation path means all outputs. A ``^`` separator opts into
        Nix's explicit canonical DerivedPath output-selection syntax.
        """
        if eval_store is not None and eval_store._session is not self._session:  # type: ignore[reportPrivateUsage] -- session ownership guard
            raise ValueError("eval_store belongs to a different inproc Session")
        mode = BuildMode.Normal.value if build_mode is None else int(build_mode)
        response = await self._session.run(
            self._require_raw().store_build_paths_with_results,
            {
                "derived_paths": [str(path) for path in derived_paths],
                "build_mode": mode,
            },
            None if eval_store is None else eval_store._require_raw(),
        )
        return [BuildResult(**result) for result in response["results"]]

    async def read_derivation(
        self, drv_path: str | PublicStorePath, /
    ) -> Derivation:
        """Parse and return the ``.drv`` file at ``drv_path``."""
        raw_path = await self._session.run(self._require_raw().parse_store_path, str(drv_path))
        result = await self._session.run(self._require_raw().read_derivation, raw_path)
        return Derivation(**result)

    async def collect_garbage(
        self,
        action: PublicGcAction,
        /,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: list[str | PublicStorePath] | tuple[()] = (),
        max_freed: int = 2**64 - 1,
    ) -> GcResult:
        """Run a garbage-collection pass; see :meth:`nanopynix.store.Store.collect_garbage`."""
        raw_paths = await self._session.run(
            _parse_store_paths, self._require_raw(), [str(p) for p in paths_to_delete]
        )
        result = await self._session.run(
            self._require_raw().collect_garbage,
            _raw_gc_action(action),
            ignore_liveness,
            raw_paths,
            max_freed,
        )
        paths = await self._session.run(
            _print_store_paths, self._require_raw(), result["paths"]
        )
        return GcResult(
            paths=[PublicStorePath(p) for p in paths],
            bytes_freed=result["bytes_freed"],
        )

    async def _public_store_paths(self, raw_paths: Any) -> list[PublicStorePath]:
        paths = await self._session.run(_print_store_paths, self._require_raw(), raw_paths)
        return [PublicStorePath(path) for path in paths]

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

    def __init__(
        self,
        session: Session,
        store: Store,
        build_store: Store | None = None,
        *,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._build_store = build_store
        self._eval_settings = eval_settings
        self._fetch_settings = fetch_settings
        self._local: LocalEvalState | None = None
        self._active = False
        self._locked_flakes: set[LockedFlake] = set()
        self._executor = NixThreadExecutor(
            thread_name_prefix="nix-eval",
            thread_initializer=nanopynix_expr._enter_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook
            thread_finalizer=nanopynix_expr._exit_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook
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
                thread_initializer=nanopynix_expr._enter_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook
                thread_finalizer=nanopynix_expr._exit_evaluator_thread,  # type: ignore[reportPrivateUsage] -- L1 GC thread-lifetime hook
            )
        nix_path = (
            self._eval_settings.nix_path
            if self._eval_settings is not None and self._eval_settings.nix_path is not None
            else self._session._nix_path  # type: ignore[reportPrivateUsage] -- Session owns evaluator configuration
        )
        rendered_eval = self._eval_settings.to_worker_settings() if self._eval_settings is not None else {}
        rendered_eval.pop("nix-path", None)  # applied via nix_path/searchPath, not the generic settings map
        rendered_fetch = self._fetch_settings.to_worker_settings() if self._fetch_settings is not None else {}
        try:
            self._local = await self.run(
                self._session._runtime.open_eval_state,  # type: ignore[reportPrivateUsage] -- Session owns local runtime
            self._store._require_local(),  # type: ignore[reportPrivateUsage] -- cross-class Store→EvalSession coupling
            nix_path,
            None if self._build_store is None else self._build_store._require_local(),  # type: ignore[reportPrivateUsage] -- cross-class Store→EvalSession coupling
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
        self._session._evals.add(self)  # type: ignore[reportPrivateUsage] -- Session owns evaluator lifetime tracking
        self._active = True

    async def configure(
        self,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> None:
        """Apply live-mutable eval/fetch settings to this already-open evaluator.

        Only settings Nix reads fresh on every access take effect this way
        (e.g. ``max_call_depth``, ``allowed_uris``, lint settings) —
        construction-time-snapshotted ones (``nix_path``, ``pure_eval``,
        ``restrict_eval``) require a new :class:`EvalSession`.
        """
        rendered_eval = eval_settings.to_worker_settings() if eval_settings is not None else {}
        rendered_eval.pop("nix-path", None)
        rendered_fetch = fetch_settings.to_worker_settings() if fetch_settings is not None else {}
        await self.run(self._require_local().configure, rendered_eval, rendered_fetch)

    async def close(self) -> None:
        """Release all values and locked flakes, then destroy this evaluator."""
        if not self._active:
            return
        for locked_flake in tuple(self._locked_flakes):
            await locked_flake.release()
        local = self._local
        self._local = None
        self._active = False
        self._session._evals.discard(self)  # type: ignore[reportPrivateUsage] -- Session owns evaluator lifetime tracking
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

    async def run(self, func: Any, *args: Any) -> Any:
        """Run evaluator and Value work on this evaluator's dedicated thread."""
        return await self._executor.run(
            _run_with_log_context,
            self._session._next_operation_id(),  # type: ignore[reportPrivateUsage] -- Session owns operation correlation
            func,
            args,
        )

    async def _run_closing(self, func: Any, *args: Any) -> Any:
        return await self._executor.run_closing(
            _run_with_log_context,
            self._session._next_operation_id(),  # type: ignore[reportPrivateUsage] -- Session owns operation correlation
            func,
            args,
        )

    def _require_raw(self) -> Any:
        if not self._active or self._local is None:
            raise InprocSessionClosedError("EvalSession is not open — use async with")
        return self._local.require_raw()

    def _require_local(self) -> LocalEvalState:
        if not self._active or self._local is None:
            raise InprocSessionClosedError("EvalSession is not open — use async with")
        return self._local

    async def string(self, expression: str, path: str = "<string>") -> Value:
        """Evaluate the Nix expression ``expression``.

        Args:
            expression: Nix source to evaluate.
            path: Source name attributed to ``expression`` in error messages.
        """
        local = await self.run(self._require_local().eval_string, expression, path)
        return self._track_value(local)

    async def file(self, path: str) -> Value:
        """Evaluate the Nix expression in the file at ``path``."""
        local = await self.run(self._require_local().eval_file, path)
        return self._track_value(local)

    def _track_value(self, local: LocalValue) -> Value:
        return Value(self, local)

    async def repl(self) -> ReplSession:
        """Begin a persistent Nix REPL scope over this evaluator."""
        raw = self._require_raw()
        await self.run(raw.begin_repl)
        return ReplSession(self)

    async def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
        flake_settings: NixFlakeSettings | None = None,
    ) -> LockedFlake:
        """Lock a flake, optionally retaining the lock only in memory."""
        local = await self.run(
            partial(
                self._require_local().lock_flake,
                ref,
                update_inputs=update_inputs,
                write_lock_file=write_lock_file,
                flake_settings=flake_settings.to_worker_settings() if flake_settings is not None else None,
            )
        )
        proto = await self.run(_locked_flake_proto, local.require_raw())
        locked_flake = LockedFlake(self, local, proto.description, proto.inputs)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    async def eval_flake(
        self, ref: str, *, write_lock_file: bool = True, flake_settings: NixFlakeSettings | None = None
    ) -> Value:
        """Lock and evaluate a flake in one step."""
        local = await self.run(
            partial(
                self._require_local().eval_flake,
                ref,
                write_lock_file=write_lock_file,
                flake_settings=flake_settings.to_worker_settings() if flake_settings is not None else None,
            )
        )
        return self._track_value(local)

    async def reset_file_cache(self) -> None:
        """Discard parsed file cache entries before re-evaluating source files."""
        await self.run(self._require_raw().reset_file_cache)


class ReplSession:
    """Persistent REPL scope backed by its parent direct ``EvalState``."""

    def __init__(self, eval_session: EvalSession) -> None:
        self._eval_session = eval_session

    async def line(self, text: str, path: str = "<string>") -> Value | None:
        """Process one Nix REPL line.

        A binding such as ``x = 1`` returns ``None``. An expression returns a
        session-bound :class:`Value`.
        """
        local = await self._eval_session.run(self._eval_session._require_local().repl_process_line, text, path)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→ReplSession coupling
        return None if local is None else self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def load_file(self, path: str) -> Value:
        """Load a Nix expression file as ``nix repl :load`` does."""
        local = await self._eval_session.run(self._eval_session._require_local().repl_load_file, path)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→ReplSession coupling
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def add_attrs(self, value: Value) -> list[str]:
        """Add all attributes from ``value`` to this REPL's lexical scope."""
        local_value = value._local_for(self._eval_session)  # type: ignore[reportPrivateUsage] -- same-evaluator guard
        return await self._eval_session.run(self._eval_session._require_local().repl_add_attrs, local_value)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→ReplSession coupling

    async def scope_names(self) -> list[str]:
        """Return the identifiers visible in this REPL's lexical scope."""
        return await self._eval_session.run(self._eval_session._require_raw().repl_scope_names)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→ReplSession coupling

    async def reset_file_cache(self) -> None:
        """Discard parsed file cache entries before reloading REPL sources."""
        await self._eval_session.run(self._eval_session._require_raw().reset_file_cache)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→ReplSession coupling


class LockedFlake:
    """Async façade over one thread-confined in-memory flake lock."""

    def __init__(
        self,
        eval_session: EvalSession,
        local: LocalLockedFlake,
        description: str,
        inputs: dict[str, LockedInput],
    ) -> None:
        self._eval_session = eval_session
        self._local: LocalLockedFlake | None = local
        self.description = description
        self.inputs = inputs

    def _local_for(self) -> LocalLockedFlake:
        self._eval_session._require_local()  # type: ignore[reportPrivateUsage] -- parent owns local evaluator lifetime
        if self._local is None:
            raise InprocLockedFlakeReleasedError("LockedFlake has been released")
        return self._local

    async def eval(self) -> Value:
        """Evaluate this locked flake's outputs."""
        evaluator = self._eval_session._require_local()  # type: ignore[reportPrivateUsage] -- parent owns the local evaluator
        local = await self._eval_session.run(
            evaluator.call_locked_flake,
            self._local_for(),
        )
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def write_lock_file(self) -> None:
        """Persist this locked flake's lock file to disk."""
        await self._eval_session.run(self._local_for().write_lock_file)

    async def release(self) -> None:
        """Release the underlying handle for this locked flake. Idempotent."""
        local = self._local
        self._local = None
        self._eval_session._locked_flakes.discard(self)  # type: ignore[reportPrivateUsage] -- evaluator owns facade lifetime tracking
        if local is not None:
            await self._eval_session._run_closing(local.close)  # type: ignore[reportPrivateUsage] -- flake teardown follows evaluator close ordering


class Value:
    """Async façade over a thread-confined :class:`LocalValue`."""

    def __init__(self, eval_session: EvalSession, local: LocalValue) -> None:
        self._eval_session = eval_session
        self._local: LocalValue | None = local

    async def __aenter__(self) -> Value:
        self._local_for(self._eval_session)
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Release this value's rooted L1 object. This operation is idempotent."""
        local = self._local
        self._local = None
        if local is not None:
            await self._eval_session.run(local.close)

    def _local_for(self, eval_session: EvalSession) -> LocalValue:
        self._eval_session._require_local()  # type: ignore[reportPrivateUsage] -- liveness check before pointer use
        if eval_session is not self._eval_session:
            raise ValueError("Value belongs to a different inproc EvalSession")
        if self._local is None:
            raise InprocValueReleasedError("Value has been released")
        return self._local

    async def force(self) -> Any:
        """Evaluate to WHNF and convert to a plain Python value.

        Unlike :meth:`nanopynix.ValueProxy.force`, compound types (attrsets,
        lists) are also converted directly rather than returned as lazy
        wrapper views — there is no ``ValueAttrs``/``ValueList`` equivalent
        in the in-process API.
        """
        return await self._eval_session.run(_force_to_python, self._local_for(self._eval_session))

    async def force_deep(self) -> Any:
        """Recursively evaluate and convert the entire value tree to Python."""
        return await self._eval_session.run(_force_deep_to_python, self._local_for(self._eval_session))

    async def json(self, *, copy_to_store: bool = False) -> Any:
        """Serialize this value to JSON-compatible Python objects. See :meth:`force_json`."""
        return await self._eval_session.run(self._local_for(self._eval_session).to_json, copy_to_store)

    async def force_json(self, *, copy_to_store: bool = False) -> Any:
        """Serialize this value to JSON-compatible Python objects."""
        return await self.json(copy_to_store=copy_to_store)

    async def type(self) -> str:
        """Resolve this value and return its Nix type name (e.g. ``"string"``)."""
        return await self._eval_session.run(self._local_for(self._eval_session).type_name)

    async def as_int(self) -> int:
        """Force this value and return it as ``int``. Raises if not an int."""
        return await self._eval_session.run(self._local_for(self._eval_session).as_int)

    async def as_float(self) -> float:
        """Force this value and return it as ``float``. Raises if not a float."""
        return await self._eval_session.run(self._local_for(self._eval_session).as_float)

    async def as_bool(self) -> bool:
        """Force this value and return it as ``bool``. Raises if not a bool."""
        return await self._eval_session.run(self._local_for(self._eval_session).as_bool)

    async def as_string(self) -> str:
        """Force this value and return it as ``str``. Raises if not a string."""
        return await self._eval_session.run(self._local_for(self._eval_session).as_string)

    async def realise_string(self) -> str:
        """Coerce this value to a string and realise its Nix string context."""
        return await self._eval_session.run(self._local_for(self._eval_session).realise_string)

    async def realise_argv(self) -> list[str]:
        """Coerce a Nix list to argv and realise all of its string contexts."""
        return await self._eval_session.run(self._local_for(self._eval_session).realise_argv)

    async def edit_location(self) -> tuple[str, int]:
        """Return the physical file path and line Nix would open for this value."""
        location = await self._eval_session.run(self._local_for(self._eval_session).edit_location)
        return location["path"], location["line"]

    async def attr(self, name: str) -> Value:
        """Force this value as an attrset and return attribute ``name``."""
        local = await self._eval_session.run(self._local_for(self._eval_session).attr_get, name)
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def has_attr(self, name: str) -> bool:
        """Force this value as an attrset and return whether ``name`` is present."""
        return await self._eval_session.run(self._local_for(self._eval_session).has_attr, name)

    async def list_get(self, index: int) -> Value:
        """Force this value as a list and return element ``index``."""
        local = await self._eval_session.run(self._local_for(self._eval_session).list_get, index)
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def attr_names(self) -> list[str]:
        """Force this value as an attrset and return its attribute names."""
        return await self._eval_session.run(self._local_for(self._eval_session).attr_names)

    async def list_length(self) -> int:
        """Force this value as a list and return its length."""
        return await self._eval_session.run(self._local_for(self._eval_session).list_length)

    async def call(self, argument: Value | Any) -> Value:
        """Call this value as a Nix function with a single ``argument``.

        Args:
            argument: A ``Value`` from the same ``EvalSession``, or a plain
                Python value to convert to a Nix value.
        """
        local = self._local_for(self._eval_session)
        argument_local = await self._argument_local(argument)
        result = await self._eval_session.run(local.call, argument_local)
        return self._eval_session._track_value(result)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def auto_call(self) -> Value:
        """Apply Nix top-level auto-call semantics to a function value."""
        result = await self._eval_session.run(self._local_for(self._eval_session).auto_call)
        return self._eval_session._track_value(result)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def _argument_local(self, argument: Value | Any) -> LocalValue:
        if isinstance(argument, Value):
            return argument._local_for(self._eval_session)
        return await self._eval_session.run(self._eval_session._require_local().value_from_python, argument)  # type: ignore[reportPrivateUsage] -- cross-class EvalSession→Value coupling

    async def build(self, *, store: Store | None = None, build_mode: Any = None) -> dict[str, str]:
        """Build the derivation represented by this evaluated value.

        Args:
            store: Store to build into; defaults to the store this value's
                ``EvalSession`` was opened with.
            build_mode: A :data:`BuildMode` value, or ``None`` for normal builds.
        """
        target_store = self._eval_session._store if store is None else store  # type: ignore[reportPrivateUsage] -- evaluator's bound store is default
        if target_store._session is not self._eval_session._session:  # type: ignore[reportPrivateUsage] -- session ownership guard
            raise ValueError("Store belongs to a different inproc Session")
        derived_path = await self.get_derived_path()
        results = await target_store.build_paths_with_results(
            [derived_path],
            build_mode=build_mode,
            eval_store=None if target_store is self._eval_session._store else self._eval_session._store,  # type: ignore[reportPrivateUsage] -- cross-store build source
        )
        if not results or not results[0].success:
            raise RuntimeError(results[0].error_msg if results else "build returned no result")
        derivation = await target_store.read_derivation(derived_path)
        return {
            name: output.path
            for name, output in derivation.outputs.items()
            if output.path is not None
        }

    async def get_derived_path(self) -> str:
        """Extract this derivation's canonical DerivedPath string.

        The result contains no evaluator pointer and can be built by any Store
        in this session. Plain derivation paths select all outputs by default.
        """
        return await self._eval_session.run(self._local_for(self._eval_session).derived_path)

    async def release(self) -> None:
        """Alias for :meth:`close`, matching the RPC value lifecycle API."""
        await self.close()


def _call(target: Any, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
    return target(*args, **kwargs)


def _force_to_python(value: Any) -> Any:
    value.force()
    return value.to_python()


def _force_deep_to_python(value: Any) -> Any:
    value.force_deep()
    return value.to_python()


__all__ = [
    "BuildMode",
    "EvalSession",
    "GCAction",
    "InprocLockedFlakeReleasedError",
    "InprocSessionClosedError",
    "InprocValueReleasedError",
    "LockedFlake",
    "RawEvalState",
    "RawStore",
    "RawValue",
    "ReplSession",
    "Session",
    "Store",
    "StorePath",
    "Value",
]
