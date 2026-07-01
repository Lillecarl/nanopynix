"""Substitution goals backed by configured stores."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import structlog

from ..exceptions import OpNotImplementedError
from ..serde import (
    AddToStoreNarRequest,
    BuildResultStatus,
    IsValidPathRequest,
    NarFromPathRequest,
    QueryPathInfoRequest,
)
from ..serde import StorePath as SerdeStorePath
from ..serde.context import ReadContext, WriteContext
from ..serde.valid_path_info import ValidPathInfo
from ..store import DaemonStore, HTTPBinaryCacheStore, Store
from ..store.http_binary_cache import HTTPNarInfo
from ..store_path import StorePath
from .goal import ExecutionGoal
from .results import GoalResult, goal_failure, goal_success

if TYPE_CHECKING:
    from .engine import GoalEngine

log = structlog.get_logger(__name__)
_NAR_CHUNK_SIZE = 1024 * 256


@dataclass(frozen=True)
class SubstituteAttempt:
    found: bool
    result: GoalResult


@dataclass(frozen=True)
class _SubstitutionSource:
    store: Store
    info: ValidPathInfo
    http_narinfo: HTTPNarInfo | None = None

    @property
    def references(self) -> set[StorePath]:
        return {StorePath(str(path)) for path in self.info.info.references}


class SubstitutePathGoal(ExecutionGoal[SubstituteAttempt]):
    """Substitute one store path and its reference closure."""

    def __init__(self, engine: GoalEngine, path: StorePath, substituter_ids: tuple[str, ...]) -> None:
        super().__init__(engine)
        self.path = path
        self.substituter_ids = substituter_ids

    async def _run(self) -> SubstituteAttempt:
        log.debug("substitute_path_start", path=str(self.path), substituters=self.substituter_ids)
        if await self._is_valid_local_path(self.path):
            log.debug("substitute_path_already_valid", path=str(self.path))
            result = goal_success()
            result.produced_paths.add(self.path)
            return SubstituteAttempt(found=True, result=result)

        source = await self._find_source()
        if source is None:
            log.debug("substitute_path_miss", path=str(self.path))
            return SubstituteAttempt(
                found=False,
                result=goal_failure(
                    f"pynixd: no substituter has path: {self.path}",
                    BuildResultStatus.UNKNOWN,
                ),
            )

        reference_goals: list[SubstitutePathGoal] = []
        for reference in sorted(source.references, key=str):
            if reference == self.path:
                continue
            reference_goals.append(await self.engine.get_substitute_path_goal(reference, self.substituter_ids))
        log.debug(
            "substitute_path_hit",
            path=str(self.path),
            store_id=source.store.store_id,
            references=len(reference_goals),
        )

        reference_results = await self.run_children(reference_goals)
        for reference_result in reference_results:
            if not reference_result.found:
                return SubstituteAttempt(
                    found=True,
                    result=goal_failure(
                        f"pynixd: cannot substitute {self.path}; missing reference",
                        BuildResultStatus.UNKNOWN,
                    ),
                )
            if not _result_succeeded(reference_result.result):
                return SubstituteAttempt(found=True, result=reference_result.result)

        try:
            log.debug("substitute_path_import_start", path=str(self.path), store_id=source.store.store_id)
            await self._import_nar(source)
        except Exception as exc:
            log.warning("substitute_path_failed", path=str(self.path), store_id=source.store.store_id, exc_info=True)
            return SubstituteAttempt(
                found=True,
                result=goal_failure(
                    f"pynixd: failed to substitute {self.path}: {exc}",
                    BuildResultStatus.MISC_FAILURE,
                ),
            )

        result = goal_success()
        log.debug("substitute_path_import_done", path=str(self.path), store_id=source.store.store_id)
        result.produced_paths.add(self.path)
        result.resolved_outputs["out"] = self.path
        return SubstituteAttempt(found=True, result=result)

    async def _find_source(self) -> _SubstitutionSource | None:
        stores = list(self.engine.substituter_stores())
        if not stores:
            return None

        lock = anyio.Lock()
        done = anyio.Event()
        remaining = len(stores)
        result: _SubstitutionSource | None = None

        async def try_store(store: Store) -> None:
            nonlocal remaining, result
            try:
                source = await self._try_store(store)
            except anyio.get_cancelled_exc_class():
                raise
            finally:
                async with lock:
                    remaining -= 1
                    if remaining == 0:
                        done.set()
            if source is None:
                return
            async with lock:
                if result is None:
                    result = source
                    done.set()

        async with anyio.create_task_group() as tg:
            for store in stores:
                tg.start_soon(try_store, store)
            await done.wait()
            if result is not None:
                tg.cancel_scope.cancel()
        return result

    async def _try_store(self, store: Store) -> _SubstitutionSource | None:
        try:
            if isinstance(store, HTTPBinaryCacheStore):
                narinfo = await store.get_narinfo(self.path)
                if narinfo is None:
                    return None
                return _SubstitutionSource(store=store, info=narinfo.valid_path_info, http_narinfo=narinfo)

            response = await store.execute(QueryPathInfoRequest(path=SerdeStorePath(path=str(self.path))))
            if not response.valid or response.info is None:
                return None
            return _SubstitutionSource(
                store=store,
                info=ValidPathInfo(path=SerdeStorePath(path=str(self.path)), info=response.info),
            )
        except OpNotImplementedError:
            return None
        except Exception:
            log.debug("substituter_query_failed", store_id=store.store_id, path=str(self.path), exc_info=True)
            return None

    async def _import_nar(self, source: _SubstitutionSource) -> None:
        async with self.engine.substitution_import_limiter, self.engine.ctx.local_store.transfer_conn() as conn:
            request = AddToStoreNarRequest(
                info=source.info,
                repair=0,
                dont_check_sigs=1,
            )
            await request.to_writer(WriteContext.from_conn(conn))
            await conn.w.drain()

            framed = conn.w.framed()
            if isinstance(source.store, HTTPBinaryCacheStore):
                if source.http_narinfo is None:
                    raise RuntimeError("missing HTTP narinfo for HTTP substitution source")
                async for chunk in source.store.stream_nar(source.http_narinfo):
                    framed.write(chunk)
                    await conn.w.drain()
            elif isinstance(source.store, DaemonStore):
                await self._stream_from_daemon(source.store, framed, conn)
            else:
                raise RuntimeError(f"store {source.store.store_id} cannot stream NARs")

            await framed.finalize()
            await request.response_type.from_reader(ReadContext.from_conn(conn))

    async def _stream_from_daemon(self, store: DaemonStore, framed, destination_conn) -> None:
        async with store.transfer_conn() as source_conn:
            await NarFromPathRequest(path=SerdeStorePath(path=str(self.path))).to_writer(
                WriteContext.from_conn(source_conn)
            )
            await source_conn.w.drain()
            await source_conn.r.drain_stderr()

            remaining = await self._nar_size_from(store)
            while remaining > 0:
                chunk = await source_conn.r.readexactly(min(remaining, _NAR_CHUNK_SIZE))
                framed.write(chunk)
                await destination_conn.w.drain()
                remaining -= len(chunk)

    async def _nar_size_from(self, store: Store) -> int:
        response = await store.execute(QueryPathInfoRequest(path=SerdeStorePath(path=str(self.path))))
        if not response.valid or response.info is None:
            raise RuntimeError(f"substituter lost path while streaming: {self.path}")
        return response.info.nar_size

    async def _is_valid_local_path(self, path: StorePath) -> bool:
        response = await self.engine.ctx.local_store.execute(IsValidPathRequest(path=SerdeStorePath(path=str(path))))
        return bool(response.valid)


def substituter_fingerprint(substituter_ids: tuple[str, ...]) -> str:
    payload = "\0".join(substituter_ids).encode()
    return hashlib.sha256(payload).hexdigest()


def _result_succeeded(result: GoalResult) -> bool:
    try:
        return BuildResultStatus(result.result.status).is_success
    except ValueError:
        return False
