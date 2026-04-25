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
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

import structlog

from .. import derived_path as derived_path_mod
from ..exceptions import OpNotImplementedError
from ..store_path import StorePath
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
    OperationLogs as OperationLogs,
)
from ..types import (
    OutputKind as OutputKind,
)
from ..types import (
    RequestContext as RequestContext,
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

log = structlog.get_logger(__name__)


# ── Base classes ─────────────────────────────────────────────────────

Resp = TypeVar("Resp", bound="OpResponse")

# Auto-populated by OpRequest.__init_subclass__
OP_REGISTRY: dict[int, type[OpRequest[Any]]] = {}


@dataclass
class OpRequest(ABC, Generic[Resp]):
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
        await self.from_reader(ctx.proxy.r, ctx.version)
        result = await ctx.proxy.execute(self)
        return result

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

    @abstractmethod
    async def from_reader(self, reader: NixReader, version: int) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...


@dataclass
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

    @abstractmethod
    async def from_reader(
        self,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> Self: ...

    @abstractmethod
    async def to_writer(self, writer: NixWriter, version: int) -> None: ...


# Silence BuildResult debug logs — verbose in hot paths
logging.getLogger("pynixd.types.BuildResult").setLevel(logging.WARNING)


@dataclass
class KeyedBuildResult:
    """A build result associated with its derived path."""

    derived_path: derived_path_mod.DerivedPath = field(
        default_factory=lambda: StorePath(""),
    )  # type: ignore
    result: BuildResult = field(default_factory=BuildResult)

    async def from_reader(self, reader: NixReader, version: int) -> Self:
        self.derived_path = await reader.read_string(derived_path_mod.DerivedPath)
        self.result = await BuildResult().from_reader(reader, version)
        return self

    async def to_writer(self, writer: NixWriter, version: int) -> None:
        writer.write_string(self.derived_path)
        await self.result.to_writer(writer, version)


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
        return len(self._buf) > 0

    def getvalue(self) -> bytes:
        return bytes(self._buf)
