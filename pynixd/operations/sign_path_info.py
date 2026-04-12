"""SignPathInfo operation - sign a PathInfo with configured secret keys.
This is a custom operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

import structlog

from ..signing import SecretKey, get_default_signing_key, sign_path_info
from ..wire import NixReader, NixWriter
from .add_signatures import AddSignaturesRequest
from .base import OpRequest, OpResponse, OperationLogs, PathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)


@dataclass
class SignPathInfoResponse(OpResponse):
    info: PathInfo = field(default_factory=PathInfo)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> SignPathInfoResponse:
        logs = await OperationLogs.from_reader(reader)
        info = await PathInfo.from_reader_keyed(reader)
        return cls(logs=logs, info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger.debug("to_writer", info=self.info)
        self.logs.to_writer(writer)
        await self.info.to_writer_keyed(writer)


@dataclass
class SignPathInfoRequest(OpRequest[SignPathInfoResponse]):
    name: ClassVar[str] = "SignPathInfo"
    op: ClassVar[int] = 107
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = SignPathInfoResponse
    info: PathInfo = field(default_factory=PathInfo)
    key: SecretKey | None = None

    def has_signature(self, key_name: str) -> bool:
        prefix = f"{key_name}:"
        return any(sig.startswith(prefix) for sig in self.info.sigs)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> SignPathInfoRequest:
        info = await PathInfo.from_reader_keyed(reader)
        cls.logger.debug("from_reader", path=info.path)
        return cls(info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        await self.info.to_writer_keyed(writer)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> SignPathInfoResponse:
        key = self.key or get_default_signing_key()
        if key is None:
            return SignPathInfoResponse(info=self.info)

        if self.has_signature(key.name):
            return SignPathInfoResponse(info=self.info)

        sig = sign_path_info(key, self.info)
        self.info.sigs.add(sig)

        if store.db is not None:
            async with store.db.acquire_conn() as conn:
                await conn.execute(
                    """UPDATE ValidPaths SET sigs = CASE 
                       WHEN sigs = '' THEN ? ELSE sigs || ' ' || ? END
                       WHERE path = ? AND sigs NOT LIKE ? 
                       AND sigs NOT LIKE ? AND sigs NOT LIKE ?""",
                    (
                        sig,
                        sig,
                        str(self.info.path),
                        f"%{sig}%",
                        f"{sig} %",
                        f"% {sig}",
                    ),
                )
                await conn.commit()

        await store.execute(
            AddSignaturesRequest(path=str(self.info.path), sigs={sig}),
            client=client,
            suppress_last=suppress_last,
        )

        store.add_path_info(self.info)
        return SignPathInfoResponse(info=self.info)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> SignPathInfoResponse:
        log = structlog.get_logger(f"pynixd.operations.{cls.__name__}")
        log.debug("received_op")
        request = await cls.from_reader(proxy.r, proxy.version)
        result = await proxy.execute(request)
        log.debug("responded_op")
        return result
