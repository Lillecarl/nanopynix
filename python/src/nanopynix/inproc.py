"""Asynchronous in-process Nix API backed by direct L1 object pointers.

Unlike :class:`nanopynix.Session`, this module does not start a worker
process.  It still confines all Nix work to one dedicated thread, so callers
retain the same asynchronous programming model without exposing Nix's
thread-affinity requirements.
"""

from __future__ import annotations

import asyncio
import os
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nanopynix_expr
import nanopynix_store
import nanopynix_util
from nanopynix._extract import locked_flake as _locked_flake_proto
from nanopynix._local import LocalEvalState, LocalLockedFlake, LocalRuntime, LocalStore, LocalValue
from nanopynix._nix_executor import NixThreadExecutor
from nanopynix.logging import LogCollector
from nanopynix.models import LockedInput, LogEvent
from nanopynix.settings import NixSettings, normalize_nix_settings
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

# Nix initialization is process-global and must remain on the same OS thread
# for the lifetime of this interpreter. Sessions are exclusive, but reuse this
# executor sequentially rather than creating thread-affinity violations.
_EXECUTOR = NixThreadExecutor()
_initialization_signature: tuple[object, ...] | None = None


class InprocSessionClosedError(RuntimeError):
    """Raised when an in-process session resource is used after close."""


class InprocEvalBusyError(RuntimeError):
    """Raised when a session already owns its single permitted EvalState."""


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
    time. A session likewise owns at most one live :class:`EvalSession`.
    """

    _active_session: Session | None = None

    def __init__(
        self,
        *,
        nix_conf: Path | None = None,
        load_config: bool = True,
        settings: NixSettings | PathLike[str] | str | None = None,
        experimental_features: Sequence[str] | None = None,
        verbosity: LogLevelInput | None = None,
        nix_path: str | Sequence[str] | None = None,
        pure_eval: bool | None = None,
        restrict_eval: bool | None = None,
        allowed_uris: Sequence[str] | None = None,
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
        self._pure_eval = pure_eval
        self._restrict_eval = restrict_eval
        self._allowed_uris = list(allowed_uris or [])
        self._collector = LogCollector()
        self._runtime = LocalRuntime()
        self._executor = _EXECUTOR
        self._log_callbacks: set[Any] = set()
        self._log_task: asyncio.Task[None] | None = None
        self._opened = False
        self._eval: EvalSession | None = None
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
        global _initialization_signature
        if self._opened:
            return
        active = type(self)._active_session
        if active is not None and active is not self:
            raise RuntimeError("only one nanopynix.inproc.Session may be open per process")
        type(self)._active_session = self
        try:
            signature = self._initialization_signature()
            self._check_initialization_signature(signature)
            if self._nix_conf is not None:
                os.environ["NIX_USER_CONF_FILES"] = str(self._nix_conf)
            nanopynix_util.install_logger(self._collector.callback)
            await self._executor.run(self._init_nix)
            _initialization_signature = signature
            self._opened = True
            self._log_task = asyncio.create_task(self._forward_logs())
        except BaseException:
            type(self)._active_session = None
            nanopynix_util.remove_logger()
            raise

    def _initialization_signature(self) -> tuple[object, ...]:
        return (
            self._nix_conf,
            self._load_config,
            tuple(sorted(self._settings.to_worker_settings().items())),
            self._verbosity,
            tuple(self._nix_path),
            self._pure_eval,
            self._restrict_eval,
            tuple(self._allowed_uris),
        )

    @staticmethod
    def _check_initialization_signature(signature: tuple[object, ...]) -> None:
        if _initialization_signature is not None and signature != _initialization_signature:
            raise RuntimeError("Nix is already initialized in this process with different nanopynix.inproc settings")

    def _init_nix(self) -> None:
        self._runtime.initialize(
            settings=self._settings.to_worker_settings(),
            load_config=self._load_config,
            verbosity=None if self._verbosity is None else int(self._verbosity),
            pure_eval=self._pure_eval,
            restrict_eval=self._restrict_eval,
            allowed_uris=self._allowed_uris,
        )

    async def close(self) -> None:
        if not self._opened:
            return
        eval_session = self._eval
        if eval_session is not None:
            await eval_session.close()
        for store in tuple(self._stores):
            await store.close()
        task = self._log_task
        self._log_task = None
        if task is not None:
            await self._collector.aclose()
            await task
        await self._executor.run(nanopynix_util.remove_logger)
        self._opened = False
        type(self)._active_session = None

    async def _forward_logs(self) -> None:
        async for raw in self._collector.stream():
            request_id, action, *args = raw
            event = LogEvent(request_id=request_id, action=action, args=args)
            for callback in tuple(self._log_callbacks):
                callback(event)

    def _check_open(self) -> None:
        if not self._opened:
            raise InprocSessionClosedError("inproc Session is not open — use async with")

    async def run(self, func: Any, *args: Any) -> Any:
        """Run one direct-pointer Nix operation on this session's Nix thread."""
        self._check_open()
        return await self._executor.run(func, *args)

    def unsubscribe(self, callback: Any) -> None:
        """Remove one callback previously registered with :meth:`subscribe`."""
        self._log_callbacks.discard(callback)

    def store(self, uri: str = "auto") -> Store:
        """Return a direct-pointer store context manager."""
        store = Store(self, uri)
        self._stores.add(store)
        return store

    def eval(self, store: Store) -> EvalSession:
        """Return the single pointer-backed evaluator permitted by this session."""
        if store._session is not self:  # type: ignore[reportPrivateUsage] -- identity ownership boundary
            raise ValueError("Store belongs to a different inproc Session")
        return EvalSession(self, store)

    async def get_verbosity(self) -> int:
        return await self.run(self._runtime.get_verbosity)

    async def set_verbosity(self, verbosity: LogLevelInput) -> int:
        level = normalize_log_level(verbosity)
        return await self.run(self._runtime.set_verbosity, int(level))

    async def log_stream(self) -> AsyncIterator[LogEvent]:
        queue: asyncio.Queue[LogEvent] = asyncio.Queue()
        subscription = self.subscribe(queue.put_nowait)
        try:
            while True:
                yield await queue.get()
        finally:
            subscription.unsubscribe()

    def subscribe(self, callback: Any) -> _LogSubscription:
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
        if self._local is None:
            self._local = await self._session.run(self._session._runtime.open_store, self._uri)  # type: ignore[reportPrivateUsage] -- session owns local runtime

    async def close(self) -> None:
        local = self._local
        self._local = None
        self._session._stores.discard(self)  # type: ignore[reportPrivateUsage] -- session owns store lifetime tracking
        if local is not None:
            await self._session.run(local.close)

    def _require_raw(self) -> Any:
        if self._local is None:
            raise InprocSessionClosedError("Store is not open — use async with")
        return self._local.require_raw()

    def _require_local(self) -> LocalStore:
        if self._local is None:
            raise InprocSessionClosedError("Store is not open — use async with")
        return self._local

    async def uri(self) -> str:
        return await self._session.run(self._require_raw().get_uri)

    async def store_dir(self) -> str:
        return await self._session.run(self._require_raw().get_store_dir)

    async def parse_store_path(self, path: str) -> Any:
        return await self._session.run(self._require_raw().parse_store_path, path)

    async def is_valid_path(self, path: Any) -> bool:
        return await self._session.run(self._require_raw().is_valid_path, path)

    async def query_path_info(self, path: Any) -> dict[str, Any]:
        return await self._session.run(self._require_raw().query_path_info, path)

    async def call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        """Call an L1 store method on the session's Nix thread.

        This intentionally keeps the complete L1 store surface available while
        dedicated ergonomic methods are added above it.
        """
        raw = self._require_raw()
        target = getattr(raw, method)
        return await self._session.run(_call, target, args, kwargs)


class EvalSession:
    """Own the one direct ``EvalState`` pointer permitted by a session."""

    def __init__(self, session: Session, store: Store) -> None:
        self._session = session
        self._store = store
        self._local: LocalEvalState | None = None
        self._active = False
        self._locked_flakes: set[LockedFlake] = set()

    async def __aenter__(self) -> EvalSession:
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._active:
            return
        if self._session._eval is not None:  # type: ignore[reportPrivateUsage] -- session owns the sole EvalState slot
            raise InprocEvalBusyError("inproc Session already has a live EvalState")
        self._local = await self._session.run(self._session._runtime.open_eval_state, self._store._require_local(), self._session._nix_path)  # type: ignore[reportPrivateUsage] -- session owns local runtime
        self._session._eval = self  # type: ignore[reportPrivateUsage] -- session owns the sole EvalState slot
        self._active = True

    async def close(self) -> None:
        for locked_flake in tuple(self._locked_flakes):
            await locked_flake.release()
        local = self._local
        self._local = None
        self._active = False
        if self._session._eval is self:  # type: ignore[reportPrivateUsage] -- release sole EvalState slot
            self._session._eval = None  # type: ignore[reportPrivateUsage] -- release sole EvalState slot
        if local is not None:
            await self._session.run(local.close)

    def _require_raw(self) -> Any:
        if not self._active or self._local is None:
            raise InprocSessionClosedError("EvalSession is not open — use async with")
        return self._local.require_raw()

    def _require_local(self) -> LocalEvalState:
        if not self._active or self._local is None:
            raise InprocSessionClosedError("EvalSession is not open — use async with")
        return self._local

    async def string(self, expression: str, path: str = "<string>") -> Value:
        local = await self._session.run(self._require_local().eval_string, expression, path)
        return self._track_value(local)

    async def file(self, path: str) -> Value:
        local = await self._session.run(self._require_local().eval_file, path)
        return self._track_value(local)

    def _track_value(self, local: LocalValue) -> Value:
        return Value(self, local)

    async def repl(self) -> ReplSession:
        raw = self._require_raw()
        await self._session.run(raw.begin_repl)
        return ReplSession(self)

    async def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
    ) -> LockedFlake:
        """Lock a flake, optionally retaining the lock only in memory."""
        local = await self._session.run(
            partial(
                self._require_local().lock_flake,
                ref,
                update_inputs=update_inputs,
                write_lock_file=write_lock_file,
            )
        )
        proto = await self._session.run(_locked_flake_proto, local.require_raw())
        locked_flake = LockedFlake(self, local, proto.description, proto.inputs)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    async def eval_flake(self, ref: str, *, write_lock_file: bool = True) -> Value:
        """Lock and evaluate a flake in one step."""
        local = await self._session.run(
            partial(self._require_local().eval_flake, ref, write_lock_file=write_lock_file)
        )
        return self._track_value(local)


class ReplSession:
    """Persistent REPL scope backed by its parent direct ``EvalState``."""

    def __init__(self, eval_session: EvalSession) -> None:
        self._eval_session = eval_session

    async def line(self, text: str, path: str = "<string>") -> Value | None:
        local = await self._eval_session._session.run(self._eval_session._require_local().repl_process_line, text, path)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return None if local is None else self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def load_file(self, path: str) -> Value:
        local = await self._eval_session._session.run(self._eval_session._require_local().repl_load_file, path)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def add_attrs(self, value: Value) -> list[str]:
        local_value = value._local_for(self._eval_session)  # type: ignore[reportPrivateUsage] -- same-evaluator guard
        return await self._eval_session._session.run(self._eval_session._require_local().repl_add_attrs, local_value)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def scope_names(self) -> list[str]:
        return await self._eval_session._session.run(self._eval_session._require_raw().repl_scope_names)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def reset_file_cache(self) -> None:
        await self._eval_session._session.run(self._eval_session._require_raw().reset_file_cache)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor


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
        session = self._eval_session._session  # type: ignore[reportPrivateUsage] -- parent owns the sole Nix executor
        evaluator = self._eval_session._require_local()  # type: ignore[reportPrivateUsage] -- parent owns the local evaluator
        local = await session.run(
            evaluator.call_locked_flake,
            self._local_for(),
        )
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def write_lock_file(self) -> None:
        await self._eval_session._session.run(self._local_for().write_lock_file)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def release(self) -> None:
        local = self._local
        self._local = None
        self._eval_session._locked_flakes.discard(self)  # type: ignore[reportPrivateUsage] -- evaluator owns facade lifetime tracking
        if local is not None:
            await self._eval_session._session.run(local.close)  # type: ignore[reportPrivateUsage] -- local lock must be released on the Nix thread


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
            await self._eval_session._session.run(local.close)  # type: ignore[reportPrivateUsage] -- RootValue detaches on the Nix thread

    def _local_for(self, eval_session: EvalSession) -> LocalValue:
        self._eval_session._require_local()  # type: ignore[reportPrivateUsage] -- liveness check before pointer use
        if eval_session is not self._eval_session:
            raise ValueError("Value belongs to a different inproc EvalSession")
        if self._local is None:
            raise InprocValueReleasedError("Value has been released")
        return self._local

    async def force(self) -> Any:
        return await self._eval_session._session.run(_force_to_python, self._local_for(self._eval_session))  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def force_deep(self) -> Any:
        return await self._eval_session._session.run(_force_deep_to_python, self._local_for(self._eval_session))  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def json(self, *, copy_to_store: bool = False) -> Any:
        return await self._eval_session._session.run(self._local_for(self._eval_session).to_json, copy_to_store)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def force_json(self, *, copy_to_store: bool = False) -> Any:
        """Serialize this value to JSON-compatible Python objects."""
        return await self.json(copy_to_store=copy_to_store)

    async def type(self) -> str:
        return await self._eval_session._session.run(self._local_for(self._eval_session).type_name)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def as_int(self) -> int:
        return await self._eval_session._session.run(self._local_for(self._eval_session).as_int)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def as_float(self) -> float:
        return await self._eval_session._session.run(self._local_for(self._eval_session).as_float)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def as_bool(self) -> bool:
        return await self._eval_session._session.run(self._local_for(self._eval_session).as_bool)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def as_string(self) -> str:
        return await self._eval_session._session.run(self._local_for(self._eval_session).as_string)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def realise_string(self) -> str:
        return await self._eval_session._session.run(self._local_for(self._eval_session).realise_string)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def realise_argv(self) -> list[str]:
        return await self._eval_session._session.run(self._local_for(self._eval_session).realise_argv)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def edit_location(self) -> tuple[str, int]:
        location = await self._eval_session._session.run(self._local_for(self._eval_session).edit_location)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return location["path"], location["line"]

    async def attr(self, name: str) -> Value:
        local = await self._eval_session._session.run(self._local_for(self._eval_session).attr_get, name)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def has_attr(self, name: str) -> bool:
        return await self._eval_session._session.run(self._local_for(self._eval_session).has_attr, name)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def list_get(self, index: int) -> Value:
        local = await self._eval_session._session.run(self._local_for(self._eval_session).list_get, index)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return self._eval_session._track_value(local)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def attr_names(self) -> list[str]:
        return await self._eval_session._session.run(self._local_for(self._eval_session).attr_names)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def list_length(self) -> int:
        return await self._eval_session._session.run(self._local_for(self._eval_session).list_length)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def call(self, argument: Value | Any) -> Value:
        local = self._local_for(self._eval_session)
        argument_local = await self._argument_local(argument)
        result = await self._eval_session._session.run(local.call, argument_local)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return self._eval_session._track_value(result)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def auto_call(self) -> Value:
        result = await self._eval_session._session.run(self._local_for(self._eval_session).auto_call)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        return self._eval_session._track_value(result)  # type: ignore[reportPrivateUsage] -- parent owns rooted value tracking

    async def _argument_local(self, argument: Value | Any) -> LocalValue:
        if isinstance(argument, Value):
            return argument._local_for(self._eval_session)
        return await self._eval_session._session.run(self._eval_session._require_local().value_from_python, argument)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor

    async def build(self, *, store: Store | None = None, build_mode: Any = None) -> dict[str, str]:
        target_store = self._eval_session._store if store is None else store  # type: ignore[reportPrivateUsage] -- evaluator's bound store is default
        if target_store._session is not self._eval_session._session:  # type: ignore[reportPrivateUsage] -- session ownership guard
            raise ValueError("Store belongs to a different inproc Session")
        mode = BuildMode.Normal.value if build_mode is None else int(build_mode)
        result = await self._eval_session._session.run(self._local_for(self._eval_session).build, target_store._require_local(), mode, None)  # type: ignore[reportPrivateUsage] -- parent owns Nix executor
        results = result["results"]
        if not results or not results[0]["success"]:
            raise RuntimeError(result["results"][0].get("error_msg", "build failed") if results else "build returned no result")
        return dict(result["outputs"])

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
    "InprocEvalBusyError",
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
