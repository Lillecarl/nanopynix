"""NAR (Nix Archive) format parser and serializer.

Provides structured access to NAR archives for reading, editing, and writing.
See https://nix.dev/manual/nix/2.34/protocols/nix-archive/
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO


# ═════════════════════════════════════════════════════════════════════════════
# Data model
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class NarDirectoryEntry:
    """A single entry within a NAR directory."""

    name: str
    node: NarNode


@dataclass
class NarDirectory:
    """A directory node in a NAR archive.

    Entries must be ordered by their names (Nix requirement).
    """

    entries: list[NarDirectoryEntry] = field(default_factory=list)


@dataclass
class NarRegular:
    """A regular file node in a NAR archive."""

    contents: bytes = b""
    executable: bool = False


@dataclass
class NarSymlink:
    """A symbolic link node in a NAR archive."""

    target: str = ""


NarNode = NarDirectory | NarRegular | NarSymlink


# ═════════════════════════════════════════════════════════════════════════════
# Low-level: padded string encoding
# ═════════════════════════════════════════════════════════════════════════════


def _pad_size(n: int) -> int:
    """Return number of padding bytes needed for 8-byte alignment."""
    return (8 - (n % 8)) % 8


def _write_padded_string(buf: io.BytesIO, s: str) -> None:
    """Write a length-prefixed, null-padded ASCII string."""
    encoded = s.encode("ascii")
    length = len(encoded)
    buf.write(struct.pack("<Q", length))
    buf.write(encoded)
    buf.write(b"\x00" * _pad_size(length))


def _read_padded_string(data: bytes, offset: int) -> tuple[str, int]:
    """Read a length-prefixed, null-padded ASCII string.

    Returns (string, new_offset).
    """
    length = struct.unpack_from("<Q", data, offset)[0]
    offset += 8
    body = data[offset : offset + length].decode("ascii")
    offset += length + _pad_size(length)
    return body, offset


# ═════════════════════════════════════════════════════════════════════════════
# Writing
# ═════════════════════════════════════════════════════════════════════════════


def _write_node(buf: io.BytesIO, node: NarNode) -> None:
    _write_padded_string(buf, "(")
    _write_padded_string(buf, "type")

    if isinstance(node, NarDirectory):
        _write_padded_string(buf, "directory")
        for entry in node.entries:
            _write_padded_string(buf, "entry")
            _write_padded_string(buf, "(")
            _write_padded_string(buf, "name")
            _write_padded_string(buf, entry.name)
            _write_padded_string(buf, "node")
            _write_node(buf, entry.node)
            _write_padded_string(buf, ")")
        # Directory terminator: a single ")" padded string
        _write_padded_string(buf, ")")

    elif isinstance(node, NarRegular):
        _write_padded_string(buf, "regular")
        if node.executable:
            _write_padded_string(buf, "executable")
            _write_padded_string(buf, "")
        _write_padded_string(buf, "contents")
        length = len(node.contents)
        buf.write(struct.pack("<Q", length))
        buf.write(node.contents)
        buf.write(b"\x00" * _pad_size(length))
        _write_padded_string(buf, ")")

    elif isinstance(node, NarSymlink):
        _write_padded_string(buf, "symlink")
        _write_padded_string(buf, "target")
        _write_padded_string(buf, node.target)
        _write_padded_string(buf, ")")
    else:
        assert_never(node)


def write_nar(node: NarNode) -> bytes:
    """Serialize a NAR node to raw NAR archive bytes."""
    buf = io.BytesIO()
    _write_padded_string(buf, "nix-archive-1")
    _write_node(buf, node)
    return buf.getvalue()


def write_nar_to_path(node: NarNode, path: str | Path) -> None:
    """Serialize a NAR node to a file."""
    Path(path).write_bytes(write_nar(node))


# ═════════════════════════════════════════════════════════════════════════════
# Reading
# ═════════════════════════════════════════════════════════════════════════════


def _expect_padded_string(data: bytes, offset: int, expected: str) -> int:
    """Read a padded string and assert it equals *expected*.

    Returns the new offset.
    """
    body, offset = _read_padded_string(data, offset)
    if body != expected:
        msg = f"Expected {expected!r} at offset {offset - len(body) - 8}, got {body!r}"
        raise ValueError(msg)
    return offset


def _read_node(data: bytes, offset: int) -> tuple[NarNode, int]:
    """Read a single NAR node.

    Returns (node, new_offset).
    """
    offset = _expect_padded_string(data, offset, "(")
    offset = _expect_padded_string(data, offset, "type")

    type_val, offset = _read_padded_string(data, offset)

    if type_val == "directory":
        entries: list[NarDirectoryEntry] = []
        while True:
            kind, offset = _read_padded_string(data, offset)
            if kind == ")":
                # Directory terminator — no closing ')' at node level
                return NarDirectory(entries=entries), offset
            if kind != "entry":
                raise ValueError(f"Expected 'entry' or ')' in directory, got {kind!r}")

            offset = _expect_padded_string(data, offset, "(")
            offset = _expect_padded_string(data, offset, "name")
            name, offset = _read_padded_string(data, offset)
            offset = _expect_padded_string(data, offset, "node")
            child, offset = _read_node(data, offset)
            offset = _expect_padded_string(data, offset, ")")
            entries.append(NarDirectoryEntry(name=name, node=child))

    elif type_val == "regular":
        executable = False
        # Read attributes until we hit "contents"
        while True:
            key, offset = _read_padded_string(data, offset)
            if key == "contents":
                break
            if key == "executable":
                # Executable attribute value is always empty string
                _, offset = _read_padded_string(data, offset)
                executable = True
            else:
                raise ValueError(f"Unknown regular file attribute: {key!r}")

        # File data
        length = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        contents = data[offset : offset + length]
        offset += length + _pad_size(length)

        offset = _expect_padded_string(data, offset, ")")
        return NarRegular(contents=contents, executable=executable), offset

    elif type_val == "symlink":
        offset = _expect_padded_string(data, offset, "target")
        target, offset = _read_padded_string(data, offset)
        offset = _expect_padded_string(data, offset, ")")
        return NarSymlink(target=target), offset

    else:
        raise ValueError(f"Unknown NAR node type: {type_val!r}")


def parse_nar(data: bytes) -> NarNode:
    """Parse raw NAR archive bytes into a structured representation."""
    if len(data) < 8:
        raise ValueError("NAR data too short")
    magic, offset = _read_padded_string(data, 0)
    if magic != "nix-archive-1":
        raise ValueError(f"Invalid NAR magic: {magic!r}")
    node, offset = _read_node(data, offset)
    if offset != len(data):
        raise ValueError(f"Trailing bytes after NAR archive: {len(data) - offset} bytes")
    return node


def parse_nar_from_path(path: str | Path) -> NarNode:
    """Parse a NAR archive from a file."""
    return parse_nar(Path(path).read_bytes())


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════


def _walk(node: NarNode, prefix: str = "") -> Iterator[tuple[str, NarNode]]:
    """Yield (path, node) pairs for every node in the archive."""
    yield prefix, node
    if isinstance(node, NarDirectory):
        for entry in node.entries:
            child_prefix = f"{prefix}/{entry.name}" if prefix else entry.name
            yield from _walk(entry.node, child_prefix)


def nar_entries(node: NarNode) -> Iterator[tuple[str, NarNode]]:
    """Iterate over all entries in a NAR archive.

    Yields (path, node) pairs where *path* is the relative path within the
    archive and *node* is the NAR node at that path.
    """
    return _walk(node)


def find_nar_entry(node: NarNode, path: str) -> NarNode | None:
    """Find a NAR node by its path within the archive.

    Returns *None* if the path does not exist.
    """
    for p, n in _walk(node):
        if p == path:
            return n
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Streaming NAR support (parenthesis tracking)
# ═════════════════════════════════════════════════════════════════════════════


class NarForwarder:
    """Stateful NAR forwarder that tracks parenthesis depth.

    Feed raw bytes in, get forwarding chunks out.  Stops automatically
    when the NAR archive is complete (``self.complete`` becomes ``True``).

    Usage example::

        forwarder = NarForwarder()
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            for forwarded in forwarder.feed(chunk):
                destination.write(forwarded)
            if forwarder.complete:
                break
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._depth = 0
        self._after_contents = False
        self._body_len = 0
        self._in_body = False
        self.complete = False

    def _consume_token(self) -> bytes | None:
        """Try to consume one complete token from the internal buffer.

        Returns the raw token bytes or *None* if not enough data is buffered.
        """
        if not self._in_body:
            if len(self._buf) < 8:
                return None
            self._body_len = struct.unpack("<Q", self._buf[:8])[0]
            self._in_body = True

        pad = _pad_size(self._body_len)
        need = 8 + self._body_len + pad
        if len(self._buf) < need:
            return None

        token = bytes(self._buf[:need])
        del self._buf[:need]
        self._in_body = False
        return token

    def _update_depth(self, body: bytes) -> None:
        """Update parser state based on the token body."""
        if self._after_contents:
            self._after_contents = False
            return

        try:
            tok = body.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            return

        if tok == "(":
            self._depth += 1
        elif tok == ")":
            self._depth -= 1
            if self._depth == 0:
                self.complete = True
        elif tok == "contents":
            self._after_contents = True

    def feed(self, data: bytes) -> Iterator[bytes]:
        """Consume *data* and yield forwarding chunks.

        Each yielded chunk is a complete NAR token (length header + body +
        padding) that can be written to the destination unchanged.  When the
        archive is complete ``self.complete`` is set and no more tokens will
        be yielded.
        """
        if self.complete:
            return

        self._buf.extend(data)

        while True:
            token = self._consume_token()
            if token is None:
                return
            yield token

            body = token[8 : 8 + self._body_len]
            self._update_depth(body)

            if self.complete:
                return


def forward_nar(src: BinaryIO, dst: BinaryIO) -> int:
    """Copy a NAR archive from *src* to *dst*, returning total bytes copied.

    Reads until the self-terminating NAR structure is complete, so the
    caller does not need to know the NAR size in advance.
    """
    forwarder = NarForwarder()
    total = 0
    while True:
        chunk = src.read(65536)
        if not chunk:
            break
        for token in forwarder.feed(chunk):
            dst.write(token)
            total += len(token)
        if forwarder.complete:
            break
    return total


def nar_size(data: bytes) -> int:
    """Return the byte size of the NAR archive at the start of *data*.

    Raises *ValueError* if *data* is too short or does not contain a
    complete NAR.
    """
    forwarder = NarForwarder()
    offset = 0
    for token in forwarder.feed(data):
        offset += len(token)
    if forwarder.complete:
        return offset
    raise ValueError("Incomplete NAR archive in buffer")
