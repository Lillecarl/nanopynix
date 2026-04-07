"""BuildPaths operation request/response types."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..derived_path import DerivedPath
from ..protocol import Op, op_log
from ..wire import NixReader, NixWriter
from .base import (
    BuildMode,
    OpRequest,
    OpResponse,
    Uint64Response,
)

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

log = structlog.get_logger(__name__)


@dataclass
class BuildPathsRequest(OpRequest[Uint64Response]):
    op: ClassVar[int] = Op.BuildPaths
    response_type: ClassVar[type[OpResponse]] = Uint64Response
    is_build: ClassVar[bool] = True
    derived_paths: set[DerivedPath] = field(default_factory=set)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            derived_paths=await reader.read_string_set(DerivedPath),
            build_mode=BuildMode(await reader.read_uint64()),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string_set(self.derived_paths)
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            return await proxy.local_store.execute(request, client=proxy.client)

        from .build_derivation import BuildDerivationResponse
        from .build_planner import decompose_build_paths

        op_log("BuildPaths").debug(
            "BuildPaths len(paths)=%d", len(request.derived_paths)
        )
        decomposed = await decompose_build_paths(
            request,
            proxy.local_store,
            proxy.scheduler,
            client=proxy.client,
        )

        if not decomposed:
            return Uint64Response(value=0)

        futures = [f for _, _, f in decomposed]
        responses = await asyncio.gather(*futures)

        for resp in responses:
            if isinstance(resp, BuildDerivationResponse):
                if resp.result.status != 0:
                    return Uint64Response(value=1)

        return Uint64Response(value=0)
