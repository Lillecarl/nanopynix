"""Chunked data and file iterators for streaming payloads."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from grpclib_transports.protocol import DEFAULT_TUNING

if TYPE_CHECKING:
    from collections.abc import Iterator


def iter_chunks(data: bytes | bytearray | memoryview, chunk_size: int) -> Iterator[bytes]:
    """Yield *chunk_size*-byte slices of *data*."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    view = memoryview(data)
    for offset in range(0, len(view), chunk_size):
        yield view[offset : offset + chunk_size].tobytes()


def iter_file_chunks(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_TUNING.transfer_chunk_size,
) -> Iterator[bytes]:
    """Yield *chunk_size*-byte chunks from the file at *path*."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    with Path(path).open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk
