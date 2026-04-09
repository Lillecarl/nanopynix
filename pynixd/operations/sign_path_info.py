"""SignPathInfo operation - sign a PathInfo with configured secret keys.
This is a custom operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

import structlog

from ..exceptions import OpNotImplementedError
from ..protocol import Op
from ..signing import SecretKey, get_default_signing_key, sign_path_info
from ..wire import NixReader, NixWriter
from .add_signatures import AddSignaturesRequest
from .base import OpRequest, OpResponse, PathInfo

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..local_store_db import LocalStoreDB
    from ..proxy import DaemonProxy
    from ..store import Store

log = structlog.get_logger(__name__)

SIGN_PATH_INFO = 107


@dataclass
class SignPathInfoResponse(OpResponse):
    info: PathInfo = field(default_factory=PathInfo)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> SignPathInfoResponse:
        info = await PathInfo.from_reader_keyed(reader)
        return cls(info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        await self.info.to_writer_keyed(writer)


@dataclass
class SignPathInfoRequest(OpRequest[SignPathInfoResponse]):
    op: ClassVar[int] = Op.SignPathInfo
    response_type: ClassVar[type[OpResponse]] = SignPathInfoResponse
    info: PathInfo = field(default_factory=PathInfo)
    key: SecretKey | None = None

    def has_signature(self, key_name: str) -> bool:
        prefix = f"{key_name}:"
        return any(sig.startswith(prefix) for sig in self.info.sigs)

    @classmethod
    async def from_reader(cls, reader: NixReader, version: int) -> SignPathInfoRequest:
        info = await PathInfo.from_reader_keyed(reader)
        return cls(info=info)

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_uint64(self.op)
        await self.info.to_writer_keyed(writer)

    async def execute_db(self, db: LocalStoreDB) -> SignPathInfoResponse | None:
        key = self.key or get_default_signing_key()
        if key is None:
            return SignPathInfoResponse(info=self.info)

        if self.has_signature(key.name):
            return SignPathInfoResponse(info=self.info)

        sig = sign_path_info(key, self.info)
        self.info.sigs.add(sig)
        async with db.acquire_conn() as conn:
            # Update only if the signature is not already present
            # Note: self.info.sigs is a set, and we just added 'sig' to it.
            # The current logic appends ALL sigs in a loop which is redundant.
            # Let's just update with the NEW signature.
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

        return SignPathInfoResponse(info=self.info)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> SignPathInfoResponse:
        try:
            res = await super().execute(store, client, suppress_last)
            if res.info.sigs != self.info.sigs:
                return res
        except OpNotImplementedError:
            pass

        key = self.key or get_default_signing_key()
        if key is None:
            return SignPathInfoResponse(info=self.info)

        if self.has_signature(key.name):
            return SignPathInfoResponse(info=self.info)

        sig = sign_path_info(key, self.info)
        self.info.sigs.add(sig)

        await store.execute(
            AddSignaturesRequest(path=str(self.info.path), sigs={sig}),
            client=client,
            suppress_last=suppress_last,
        )

        store.add_path_info(self.info)
        return SignPathInfoResponse(info=self.info)

    @classmethod
    async def handle(cls, proxy: DaemonProxy) -> SignPathInfoResponse:

        structlog.contextvars.bind_contextvars(operation=cls.__name__)
        request = await cls.from_reader(proxy.r, proxy.version)
        return await proxy.execute(request)
