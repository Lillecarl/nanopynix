"""Execution context for daemon operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..connection import ClientConn
    from ..proxy import DaemonProxy
    from ..wire import NixReader, NixWriter
    from .auth import Role


@dataclass(frozen=True)
class RequestContext:
    """Context passed to operation handlers."""

    proxy: DaemonProxy
    role: Role
    version: int
    username: str


@dataclass(frozen=True)
class ReadContext:
    """Bundles the arguments needed to deserialize a response from the wire.

    Wrapping these into a single object lets migrated types accept one
    argument instead of four, while the compatibility dispatcher in
    ``OpResponse`` still calls the old ``from_reader`` signature for
    types that haven't been migrated yet.
    """

    reader: NixReader
    version: int
    client: ClientConn | None = None
    buffer_logs: bool = True

    @classmethod
    def from_call(
        cls,
        reader: NixReader,
        version: int,
        client: ClientConn | None = None,
        buffer_logs: bool = True,
    ) -> ReadContext:
        """Create a ReadContext from the old-style positional arguments.

        This lets call sites keep passing the old signature while
        migrated types read from the context object instead.
        """
        return cls(reader=reader, version=version, client=client, buffer_logs=buffer_logs)


@dataclass(frozen=True)
class WriteContext:
    """Bundles the arguments needed to serialize a request/response to the wire."""

    writer: NixWriter
    version: int

    @classmethod
    def from_call(cls, writer: NixWriter, version: int) -> WriteContext:
        return cls(writer=writer, version=version)
