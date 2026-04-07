"""BuildDerivation operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..protocol import Op
from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    OpRequest,
    OpResponse,
)

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

log = structlog.get_logger(__name__)


@dataclass
class BuildDerivationResponse(OpResponse):
    result: BuildResult = field(default_factory=BuildResult)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(result=await BuildResult.from_reader(reader, version))

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        await self.result.to_writer(writer, version)


@dataclass
class BuildDerivationRequest(OpRequest[BuildDerivationResponse]):
    op: ClassVar[int] = Op.BuildDerivation
    response_type: ClassVar[type[OpResponse]] = BuildDerivationResponse
    is_build: ClassVar[bool] = True
    drv_path: StorePath = field(default_factory=lambda: StorePath(""))
    derivation: BasicDerivation = field(default_factory=BasicDerivation)
    build_mode: BuildMode = BuildMode.NORMAL

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        return cls(
            drv_path=await reader.read_string(StorePath),
            derivation=await BasicDerivation.from_reader(reader, version),
            build_mode=BuildMode(await reader.read_uint64()),
        )

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.drv_path)
        await self.derivation.to_writer(writer, version)
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            return await proxy.local_store.execute(request, client=proxy.client)

        # Discover paths that exist on the local store but aren't tracked.
        unknown = (
            set(request.derivation.input_srcs) | {request.drv_path}
        ) - proxy.local_store.known_paths
        if unknown:
            from .query_valid_paths import QueryValidPathsRequest

            valid_resp = await proxy.local_store.execute(
                QueryValidPathsRequest(paths=unknown)
            )
            proxy.local_store.add_known_paths(valid_resp.paths, update_regtime=False)

        from .query_closure import QueryClosureRequest

        existing_inputs = request.derivation.input_srcs & proxy.local_store.known_paths
        unbuilt_inputs = request.derivation.input_srcs - proxy.local_store.known_paths

        closure_resp = await proxy.local_store.execute(
            QueryClosureRequest(paths=existing_inputs)
        )

        request.derivation.input_srcs = closure_resp.paths | unbuilt_inputs

        required_paths = set(request.derivation.input_srcs) | {request.drv_path}
        build_id, future = await proxy.scheduler.enqueue(
            Op.BuildDerivation,
            request,
            proxy.client,
            required_paths,
            platform=request.derivation.platform,
        )
        log.info(
            "build_derivation_enqueued",
            build_id=build_id,
            drv_path=request.drv_path,
        )
        response = await future

        if isinstance(response, BuildDerivationResponse):
            if response.result.status != 0 and response.result.error_msg:
                from ..stderr import StderrNext

                proxy.client.queue.put_nowait(
                    StderrNext(text=f"pynixd: {response.result.error_msg}\n")
                )
        return response
