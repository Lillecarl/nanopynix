"""BuildDerivation operation request/response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Self

from ..stderr import StderrNext
from ..store_path import StorePath
from .base import (
    BasicDerivation,
    BuildMode,
    BuildResult,
    OpRequest,
    OpResponse,
    RequestContext,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..wire import NixReader, NixWriter


@dataclass
class BuildDerivationResponse(OpResponse):
    result: BuildResult = field(default_factory=BuildResult)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        await self.logs.from_reader(reader, client=client, buffer=buffer_logs)
        self.result = await BuildResult().from_reader(reader, version)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
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

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.drv_path = await reader.read_string(StorePath)
        self.derivation = await BasicDerivation().from_reader(reader, version)
        self.build_mode = BuildMode(await reader.read_uint64())
        self.logger.debug(
            "from_reader",
            drv_path=self.drv_path,
            build_mode=self.build_mode,
        )
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        writer.write_string(self.drv_path)
        await self.derivation.to_writer(writer, version)
        writer.write_uint64(self.build_mode)

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        self.logger.debug("received_op")

        await self.from_reader(ctx.proxy.r, ctx.version)

        if not ctx.proxy.use_scheduler_for_builds:
            self.logger.debug("handle_local_mode_fallback")
            result = await ctx.proxy.local_store.execute(self, client=ctx.proxy.client)

            # Track newly built paths
            if result.result.status == 0:
                for output in result.result.built_outputs.values():
                    ctx.proxy.local_store.tracker.add_known_path(
                        StorePath(output["outPath"]).with_store_prefix(),
                    )

            self.logger.debug("responded_op")
            return result

        # The client provides a complete build recipe in BuildDerivation.
        # input_srcs contains all required dependencies (sources and other .drvs).
        # We don't need to perform extra discovery or closure expansion.
        drv_path_str = str(self.drv_path)
        required_paths: set[StorePath] = {
            StorePath(inp, extrainfo=f"input_src of {drv_path_str}") for inp in self.derivation.input_srcs
        }

        # We DO NOT add request.drv_path to required_paths because the client
        # provides the derivation contents over the wire and often doesn't
        # upload the .drv file itself to the remote builder.
        assert ctx.proxy.scheduler is not None
        build_id, future = await ctx.proxy.scheduler.build_derivation(
            self,
            ctx.proxy.client,
            required_paths,
            platform=self.derivation.platform,
        )
        self.logger.info(
            "build_derivation_enqueued",
            build_id=build_id,
            drv_path=self.drv_path,
            required_count=len(required_paths),
        )
        response = await future

        if (
            isinstance(response, BuildDerivationResponse)
            # Only send StderrNext if the error came from a backend daemon
            # (response has real daemon logs). Scheduler-generated
            # incompatibility errors (empty logs) are already sent as
            # StderrNext by the scheduler — avoid double-reporting.
            and response.result.status != 0
            and response.result.error_msg
            and response.logs.messages
        ):
            await ctx.proxy.client.queue.put(
                StderrNext(text=f"pynixd: {response.result.error_msg}\n"),
            )
        self.logger.debug("responded_op")
        return response
