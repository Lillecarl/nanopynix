"""BuildDerivation operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..store_path import StorePath
from ..wire import NixReader, NixWriter
from .base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    OpRequest,
    OpResponse,
    OperationLogs,
)
from ..stderr import StderrNext

if TYPE_CHECKING:
    from ..proxy import DaemonProxy

log = structlog.get_logger(__name__)


@dataclass
class BuildDerivationResponse(OpResponse):
    result: BuildResult = field(default_factory=BuildResult)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self:
        logs = await OperationLogs.from_reader(reader)
        result = await BuildResult.from_reader(reader, version)
        return cls(logs=logs, result=result)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", result=self.result)
        self.logs.to_writer(writer)
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
        drv_path = await reader.read_string(StorePath)
        derivation = await BasicDerivation.from_reader(reader, version)
        build_mode = BuildMode(await reader.read_uint64())
        cls.logger.debug("from_reader", drv_path=drv_path, build_mode=build_mode)
        return cls(
            drv_path=drv_path,
            derivation=derivation,
            build_mode=build_mode,
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

        # The client provides a complete build recipe in BuildDerivation.
        # input_srcs contains all required dependencies (sources and other .drvs).
        # We don't need to perform extra discovery or closure expansion.
        drv_path_str = str(request.drv_path)
        required_paths: set[StorePath] = {
            StorePath(inp, extrainfo=f"input_src of {drv_path_str}")
            for inp in request.derivation.input_srcs
        }

        # We DO NOT add request.drv_path to required_paths because the client
        # provides the derivation contents over the wire and often doesn't
        # upload the .drv file itself to the remote builder.
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
            required_count=len(required_paths),
        )
        response = await future

        if isinstance(response, BuildDerivationResponse):
            if response.result.status != 0 and response.result.error_msg:
                proxy.client.queue.put_nowait(
                    StderrNext(text=f"pynixd: {response.result.error_msg}\n")
                )
        log.debug("responded_op")
        return response
