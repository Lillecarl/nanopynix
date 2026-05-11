"""Probe a store for supported systems via BuildDerivation.

An internal operation — never dispatched from the daemon wire protocol.
Constructs trivial derivations for each candidate system and checks which
are accepted by the scheduling gate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

import structlog

from ..operations.base import (
    BasicDerivation,
    BuildMode,
    BuildResultStatus,
    DerivationOutput,
    OpRequest,
    OpResponse,
)
from ..operations.build_derivation import BuildDerivationRequest
from ..stderr import OperationLogs
from ..store_path import StorePath
from ..system_features import PROBE_SYSTEMS
from ..utils import random_nix32_hash

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types.context import ReadContext, WriteContext
    from ..wire import NixReader, NixWriter

log = structlog.get_logger(__name__)


@dataclass
class ProbeSystemsResponse(OpResponse):
    systems: set[str]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,  # noqa: ARG003
        version: int,  # noqa: ARG003
        client: ClientConn | None = None,  # noqa: ARG003
        buffer_logs: bool = True,  # noqa: ARG003
    ) -> Self:
        return cls.__new__(cls)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        pass

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.logs = OperationLogs()
        obj.systems = set()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)


@dataclass(kw_only=True)
class ProbeSystemsRequest(OpRequest[ProbeSystemsResponse]):
    name: ClassVar[str] = "ProbeSystems"
    op: ClassVar[int] = 108
    response_type: ClassVar[type[OpResponse]] = ProbeSystemsResponse
    systems: set[str]

    @classmethod
    async def from_reader(
        cls,
        reader: NixReader,  # noqa: ARG003
        version: int,  # noqa: ARG003
    ) -> Self:
        return cls.__new__(cls)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        pass

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        obj = cls.__new__(cls)
        obj.logger = cls.logger.bind(identifier=ctx.reader.identifier)
        obj.systems = set()
        return obj

    async def serialize(self, ctx: WriteContext) -> None:
        self.logger = self.logger.bind(identifier=ctx.writer.identifier)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> ProbeSystemsResponse:
        candidates = self.systems or PROBE_SYSTEMS

        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(
                    _send_probe(
                        store,
                        f"probe-system-{s}",
                        s,
                        "",
                        ["-c", f"echo {s} > $out"],
                    )
                )
                for s in candidates
            ]

        systems = {system for system, t in zip(candidates, tasks, strict=True) if t.result()[1]}

        log.info("systems_probed", store_id=store.store_id, systems=sorted(systems))
        return ProbeSystemsResponse(systems=systems)


async def _send_probe(
    store: Store,
    name: str,
    system: str,
    required_features: str,
    args: list[str],
    extra_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    drv_hash = random_nix32_hash()
    out_path = f"/nix/store/{drv_hash}-{name}"
    drv_path = StorePath(f"/nix/store/{drv_hash}-{name}.drv")

    env: dict[str, str] = {
        "builder": "/bin/sh",
        "name": name,
        "out": out_path,
        "system": system,
        "hash": drv_hash,
    }
    if required_features:
        env["requiredSystemFeatures"] = required_features
    if extra_env:
        env.update(extra_env)

    basic = BasicDerivation(
        outputs={"out": DerivationOutput(path=out_path, method="", hash_digest="")},
        input_srcs=set(),
        platform=system,
        builder="/bin/sh",
        args=args,
        env=env,
    )
    request = BuildDerivationRequest(
        drv_path=drv_path,
        derivation=basic,
        build_mode=BuildMode.NORMAL,
    )
    try:
        # use store.call to skip the scheduler, just send builds to the store
        resp = await store.call(request, skip_probe=True)
        accepted = resp.result.status in (
            BuildResultStatus.BUILT,
            BuildResultStatus.SUBSTITUTED,
            BuildResultStatus.ALREADY_VALID,
            BuildResultStatus.RESOLVES_TO_ALREADY_VALID,
        )
        if accepted:
            log.debug("probe_accepted", store_id=store.store_id, probe=name)
        else:
            log.debug(
                "probe_denied",
                store_id=store.store_id,
                probe=name,
                status=resp.result.status,
                error_msg=resp.result.error_msg,
            )
    except Exception as e:
        log.debug("probe_exception", store_id=store.store_id, probe=name, error=str(e))
        return name, False
    else:
        return name, accepted
