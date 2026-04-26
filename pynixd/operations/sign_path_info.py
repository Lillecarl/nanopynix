"""SignPathInfo operation - sign a ValidPathInfo with configured secret keys.
This is a custom operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from ..signing import SecretKey, get_default_signing_key, sign_path_info
from .add_signatures import AddSignaturesRequest
from .base import (
    OperationLogs,
    OpRequest,
    OpResponse,
    RequestContext,
    Role,
    ValidPathInfo,
)

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..wire import NixReader, NixWriter


@dataclass
class SignPathInfoResponse(OpResponse):
    info: ValidPathInfo | None = None

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.logs = await OperationLogs().from_reader(
            reader,
            client=client,
            buffer=buffer_logs,
        )
        self.info = await ValidPathInfo().from_reader(reader)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        self.logger.debug("to_writer", info=self.info)
        self.logs.to_writer(writer)
        if self.info is not None:
            self.info.to_writer(writer)


@dataclass
class SignPathInfoRequest(OpRequest[SignPathInfoResponse]):
    name: ClassVar[str] = "SignPathInfo"
    op: ClassVar[int] = 107
    is_extension: ClassVar[bool] = True
    response_type: ClassVar[type[OpResponse]] = SignPathInfoResponse
    info: ValidPathInfo | None = None
    key: SecretKey | None = None

    def has_signature(self, key_name: str) -> bool:
        if self.info is None:
            return False
        prefix = f"{key_name}:"
        return any(sig.startswith(prefix) for sig in self.info.sigs)

    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self:
        self.logger = self.logger.bind(identifier=reader.identifier)
        self.info = await ValidPathInfo().from_reader(reader)
        self.logger.debug("from_reader", path=self.info.path)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        self.logger = self.logger.bind(identifier=writer.identifier)
        writer.write_uint64(self.op)
        if self.info is not None:
            self.info.to_writer(writer)

    async def handle(self, ctx: RequestContext) -> SignPathInfoResponse | None:
        self.logger.debug("received_op")

        # Must always consume the request to keep protocol in sync
        await self.from_reader(ctx.proxy.r, ctx.version)

        if ctx.role < Role.ADMIN:
            self.logger.warning("access_denied", user=ctx.username, role=ctx.role.name)
            await ctx.proxy.send_error(
                f"Operation '{self.name}' requires administrative privileges.",
            )
            return None

        result = await ctx.proxy.execute(self)
        self.logger.debug("responded_op")
        return result

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> SignPathInfoResponse:
        if self.info is None:
            return SignPathInfoResponse(info=None)

        key = self.key or get_default_signing_key()
        if key is None:
            return SignPathInfoResponse(info=self.info)

        if self.has_signature(key.name):
            return SignPathInfoResponse(info=self.info)

        sig = sign_path_info(key, self.info)
        self.info.sigs.add(sig)

        if (db := store.db) is not None:
            async with db.acquire_conn() as conn:
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
        return SignPathInfoResponse(info=self.info)
