"""BuildDerivation operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import RequiredInput, StorePath
from ..wire import NixReader, NixWriter
from .base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    OpRequest,
    OpResponse,
)
from .query_closure import QueryClosureRequest
from .query_valid_paths import QueryValidPathsRequest
from ..stderr import StderrNext

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
    name: ClassVar[str] = "BuildDerivation"
    op: ClassVar[int] = 36
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
        writer.write_uint64(self.op)
        writer.write_string(self.drv_path)
        await self.derivation.to_writer(writer, version)
        writer.write_uint64(self.build_mode)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> OpResponse | None:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")
        request = await cls.from_reader(proxy.r, proxy.version)
        if proxy.scheduler is None:
            log.debug("handle_local_mode_fallback")
            result = await proxy.local_store.execute(request, client=proxy.client)
            log.debug("responded_op")
            return result

        # Discover paths that exist on the local store but aren't tracked.
        unknown = (
            set(request.derivation.input_srcs) | {request.drv_path}
        ) - proxy.local_store.known_paths
        if unknown:
            valid_resp = await proxy.local_store.execute(
                QueryValidPathsRequest(paths=unknown)
            )
            proxy.local_store.add_known_paths(valid_resp.paths, update_regtime=False)

        existing_inputs = request.derivation.input_srcs & proxy.local_store.known_paths
        unbuilt_inputs = request.derivation.input_srcs - proxy.local_store.known_paths

        closure_resp = await proxy.local_store.execute(
            QueryClosureRequest(paths=existing_inputs)
        )

        # Ensure pynixd knows about all paths in the closure (QueryValidPathsRequest.execute() adds them)
        if closure_resp.paths:
            await proxy.local_store.execute(
                QueryValidPathsRequest(paths=closure_resp.paths)
            )

        request.derivation.input_srcs = closure_resp.paths | unbuilt_inputs

        drv_path_str = str(request.drv_path)
        required_paths: set[RequiredInput] = set()
        for inp in request.derivation.input_srcs:
            required_paths.add(RequiredInput(inp, f"input_src of {drv_path_str}"))
        required_paths.add(
            RequiredInput(request.drv_path, f"drv_path of {drv_path_str}")
        )
        build_id, future = await proxy.scheduler.enqueue(
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
                proxy.client.queue.put_nowait(
                    StderrNext(text=f"pynixd: {response.result.error_msg}\n")
                )
        log.debug("responded_op")
        return response
