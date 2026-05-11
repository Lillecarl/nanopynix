"""
Base classes and common types for nix daemon operation serialization.

Each operation has a request and response dataclass with factory methods:
- from_reader(reader) — reads wire data into a Python object
- to_writer(writer)   — writes the Python object back to wire

These are the canonical way to speak the daemon protocol everywhere:
proxy, store, backend, scheduler, etc.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar

import structlog

from ..exceptions import OpNotImplementedError
from ..stderr import OperationLogs as OperationLogs
from ..types import (
    BasicDerivation as BasicDerivation,
)
from ..types import (
    BuildMode as BuildMode,
)
from ..types import (
    BuildResult as BuildResult,
)
from ..types import (
    BuildResultStatus as BuildResultStatus,
)
from ..types import (
    BuiltOutput as BuiltOutput,
)
from ..types import (
    DerivationOutput as DerivationOutput,
)
from ..types import (
    KeyedBuildResult as KeyedBuildResult,
)
from ..types import (
    OutputKind as OutputKind,
)
from ..types import (
    Role as Role,
)
from ..types import (
    SubstitutablePathInfo as SubstitutablePathInfo,
)
from ..types import (
    UnkeyedValidPathInfo as UnkeyedValidPathInfo,
)
from ..types import (
    ValidPathInfo as ValidPathInfo,
)
from ..wire import NixReader, NixWriter

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..store import Store
    from ..types import RequestContext
    from ..types.context import ReadContext, WriteContext

log = structlog.get_logger(__name__)


# ── Base classes ─────────────────────────────────────────────────────

Resp = TypeVar("Resp", bound="OpResponse")

# Auto-populated by OpRequest.__init_subclass__
OP_REGISTRY: dict[int, type[OpRequest[Any]]] = {}


@dataclass
class OpRequest[Resp: OpResponse](ABC):
    """Base class for operation requests.

    Subclasses that set ``op`` in their own class body are automatically
    registered in :data:`OP_REGISTRY` for wire-protocol dispatch.
    """

    op: ClassVar[int]
    name: ClassVar[str]
    response_type: ClassVar[type[OpResponse]]
    is_query: ClassVar[bool] = False
    is_build: ClassVar[bool] = False
    is_extension: ClassVar[bool] = False
    # logger is set per-subclass by __init_subclass__; instances override it
    # via self.logger = self.logger.bind(identifier=...) in from_reader/to_writer.
    # Declared without type annotation so dataclass ignores it.
    logger = structlog.get_logger("pynixd.operations.base")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Runs when an OpRequest subclass is instantiated, registers
        the subclass in OP_REGISTRY"""
        super().__init_subclass__(**kwargs)
        if "op" in cls.__dict__:
            OP_REGISTRY[cls.op] = cls
        cls.logger = structlog.get_logger(f"pynixd.operations.{cls.__name__}")

    async def handle(self, ctx: RequestContext) -> OpResponse | None:
        """Handle this operation from a client.

        Decodes the request and delegates execution to the stores.
        Streaming operations should override this method.
        """
        self = await self.from_reader(ctx.proxy.r, ctx.version)
        return await ctx.proxy.execute(self)

    async def execute(
        self,
        store: Store,
        client: ClientConn | None = None,
        suppress_last: bool = False,
    ) -> Resp:
        """Execute this operation on a store and return a buffered response.

        Falls back to the wire protocol (standard ops) or extension negotiation
        for extension operations.
        """
        if self.is_extension:
            await store.probe()

            feature_name = type(self).name
            if feature_name in store.features:
                from ..testing import set_test_value

                set_test_value(f"{feature_name}_delegated", True)
                return await store.call(
                    self,
                    client=client,
                    suppress_last=suppress_last,
                )

            raise OpNotImplementedError(
                f"Extension operation {type(self).__name__} (op={self.op}) "
                "not supported by this store (no DB and no wire fallback)",
            )

        return await store.call(
            self,
            client=client,
            suppress_last=suppress_last,
        )

    @classmethod
    @abstractmethod
    async def from_reader(cls, reader: NixReader, version: int) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        """New-style deserialization entry point.

        Subclasses that have been migrated override this method.
        The default implementation falls back to the old ``from_reader``
        signature so unmigrated subclasses keep working.
        """
        if "deserialize" not in cls.__dict__:
            return await cls.from_reader(ctx.reader, ctx.version)
        raise NotImplementedError(
            f"{cls.__name__}.deserialize(ctx) must be overridden"
        )

    async def serialize(self, ctx: WriteContext) -> None:
        """New-style serialization entry point.

        Subclasses that have been migrated override this method.
        The default implementation falls back to the old ``to_writer``
        signature so unmigrated subclasses keep working.
        """
        if "serialize" not in self.__class__.__dict__:
            await self.to_writer(ctx.writer, ctx.version)
            return
        raise NotImplementedError(
            f"{type(self).__name__}.serialize(ctx) must be overridden"
        )


@dataclass(kw_only=True)
class OpResponse(ABC):
    """Base class for operation responses."""

    logs: OperationLogs = field(default_factory=OperationLogs)
    # logger is set per-subclass by __init_subclass__; instances override it
    # via self.logger = self.logger.bind(identifier=...) in from_reader/to_writer.
    # Declared without type annotation so dataclass ignores it.
    logger = structlog.get_logger("pynixd.operations.base")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.logger = structlog.get_logger(f"pynixd.operations.{cls.__name__}")

    @property
    def is_not_found(self) -> bool:
        """True if this response indicates that the requested data was not found.
        Used by proxy to decide whether to try other stores.
        """
        return False

    @classmethod
    @abstractmethod
    async def from_reader(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...

    @classmethod
    async def deserialize(cls, ctx: ReadContext) -> Self:
        """New-style deserialization entry point.

        Subclasses that have been migrated override this method.
        The default implementation falls back to the old ``from_reader``
        signature so unmigrated subclasses keep working.
        """
        if "deserialize" not in cls.__dict__:
            return await cls.from_reader(
                ctx.reader, ctx.version,
                client=ctx.client, buffer_logs=ctx.buffer_logs,
            )
        raise NotImplementedError(
            f"{cls.__name__}.deserialize(ctx) must be overridden"
        )

    async def serialize(self, ctx: WriteContext) -> None:
        """New-style serialization entry point.

        Subclasses that have been migrated override this method.
        The default implementation falls back to the old ``to_writer``
        signature so unmigrated subclasses keep working.
        """
        if "serialize" not in type(self).__dict__:
            await self.to_writer(ctx.writer, ctx.version)
            return
        raise NotImplementedError(
            f"{type(self).__name__}.serialize(ctx) must be overridden"
        )


# Silence BuildResult debug logs — verbose in hot paths
logging.getLogger("pynixd.types.BuildResult").setLevel(logging.WARNING)


# ── Helpers ──────────────────────────────────────────────────────────


class ByteCollector(NixWriter):
    """NixWriter that collects bytes into a buffer."""

    def __init__(self, identifier: str = "memory") -> None:
        super().__init__(identifier=identifier)
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        pass

    async def is_dirty(self) -> bool:
        return bool(self._buf)

    def getvalue(self) -> bytes:
        return bytes(self._buf)
