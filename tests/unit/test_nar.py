"""Unit tests for pynixd.nar — NAR format parser and serializer."""

from __future__ import annotations

import pytest

from pynixd.nar import (
    NarDirectory,
    NarDirectoryEntry,
    NarForwarder,
    NarNode,
    NarRegular,
    NarSymlink,
    find_nar_entry,
    forward_nar,
    nar_entries,
    nar_size,
    parse_nar,
    parse_nar_from_path,
    write_nar,
    write_nar_to_path,
)

# ═════════════════════════════════════════════════════════════════════════════
# 1. Roundtrip tests
# ═════════════════════════════════════════════════════════════════════════════


class TestNarRoundtrip:
    """Serialize → deserialize → compare for every node type."""

    async def test_empty_file(self):
        node = NarRegular(contents=b"")
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarRegular)
        assert result.contents == b""
        assert result.executable is False

    async def test_small_file(self):
        node = NarRegular(contents=b"hello world")
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarRegular)
        assert result.contents == b"hello world"
        assert result.executable is False

    async def test_executable_file(self):
        node = NarRegular(contents=b"#!/bin/sh\necho hi", executable=True)
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarRegular)
        assert result.contents == b"#!/bin/sh\necho hi"
        assert result.executable is True

    async def test_file_with_padding(self):
        """File sizes that are not multiples of 8 need padding."""
        for size in [1, 7, 8, 9, 15, 16, 17, 63, 64, 65]:
            contents = b"x" * size
            node = NarRegular(contents=contents)
            data = write_nar(node)
            result = parse_nar(data)
            assert isinstance(result, NarRegular)
            assert result.contents == contents

    async def test_symlink(self):
        node = NarSymlink(target="/nix/store/abc-foo/bin/hello")
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarSymlink)
        assert result.target == "/nix/store/abc-foo/bin/hello"

    async def test_empty_directory(self):
        node = NarDirectory(entries=[])
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarDirectory)
        assert result.entries == []

    async def test_directory_with_files(self):
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="a.txt", node=NarRegular(contents=b"aaa")),
                NarDirectoryEntry(name="b.txt", node=NarRegular(contents=b"bbb")),
            ]
        )
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarDirectory)
        assert len(result.entries) == 2
        assert result.entries[0].name == "a.txt"
        assert isinstance(result.entries[0].node, NarRegular)
        assert result.entries[0].node.contents == b"aaa"
        assert result.entries[1].name == "b.txt"
        assert isinstance(result.entries[1].node, NarRegular)
        assert result.entries[1].node.contents == b"bbb"

    async def test_nested_directory(self):
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(
                    name="outer",
                    node=NarDirectory(
                        entries=[
                            NarDirectoryEntry(
                                name="inner",
                                node=NarDirectory(
                                    entries=[
                                        NarDirectoryEntry(
                                            name="deep.txt",
                                            node=NarRegular(contents=b"deep"),
                                        ),
                                    ]
                                ),
                            ),
                        ]
                    ),
                ),
                NarDirectoryEntry(name="top.txt", node=NarRegular(contents=b"top")),
            ]
        )
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarDirectory)
        assert len(result.entries) == 2
        assert result.entries[0].name == "outer"
        outer = result.entries[0].node
        assert isinstance(outer, NarDirectory)
        assert len(outer.entries) == 1
        assert outer.entries[0].name == "inner"
        inner = outer.entries[0].node
        assert isinstance(inner, NarDirectory)
        assert len(inner.entries) == 1
        assert inner.entries[0].name == "deep.txt"
        assert isinstance(inner.entries[0].node, NarRegular)
        assert inner.entries[0].node.contents == b"deep"
        assert result.entries[1].name == "top.txt"
        assert isinstance(result.entries[1].node, NarRegular)
        assert result.entries[1].node.contents == b"top"

    async def test_mixed_directory(self):
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(
                    name="bin",
                    node=NarDirectory(
                        entries=[
                            NarDirectoryEntry(
                                name="hello",
                                node=NarRegular(
                                    contents=b"#!/bin/sh\necho hello",
                                    executable=True,
                                ),
                            ),
                        ]
                    ),
                ),
                NarDirectoryEntry(
                    name="lib",
                    node=NarDirectory(
                        entries=[
                            NarDirectoryEntry(
                                name="link",
                                node=NarSymlink(target="/nix/store/abc-foo/lib"),
                            ),
                        ]
                    ),
                ),
                NarDirectoryEntry(name="README", node=NarRegular(contents=b"hi")),
            ]
        )
        data = write_nar(node)
        result = parse_nar(data)
        assert isinstance(result, NarDirectory)
        assert len(result.entries) == 3


# ═════════════════════════════════════════════════════════════════════════════
# 2. Error handling
# ═════════════════════════════════════════════════════════════════════════════


class TestNarErrors:
    """Invalid NAR data should raise ValueError."""

    async def test_bad_magic(self):
        data = b"\x0enot-a-nar-\x00\x00\x00\x00\x00\x00"
        with pytest.raises(ValueError, match="Invalid NAR magic"):
            parse_nar(data)

    async def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            parse_nar(b"")

    async def test_bad_node_type(self):
        """Inject an unknown type value after the magic."""
        import io

        from pynixd.nar import _write_padded_string

        buf = io.BytesIO()
        _write_padded_string(buf, "nix-archive-1")
        _write_padded_string(buf, "(")
        _write_padded_string(buf, "type")
        _write_padded_string(buf, "unknown")
        data = buf.getvalue()

        with pytest.raises(ValueError, match="Unknown NAR node type"):
            parse_nar(data)

    async def test_trailing_bytes(self):
        node = NarRegular(contents=b"x")
        data = write_nar(node) + b"extra"
        with pytest.raises(ValueError, match="Trailing bytes"):
            parse_nar(data)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Helpers
# ═════════════════════════════════════════════════════════════════════════════


class TestNarHelpers:
    """Test walk / find helpers."""

    async def test_nar_entries(self):
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(
                    name="a",
                    node=NarDirectory(
                        entries=[
                            NarDirectoryEntry(name="b.txt", node=NarRegular(contents=b"b")),
                        ]
                    ),
                ),
                NarDirectoryEntry(name="c.txt", node=NarRegular(contents=b"c")),
            ]
        )
        paths = {p for p, _ in nar_entries(node)}
        assert paths == {"", "a", "a/b.txt", "c.txt"}

    async def test_find_nar_entry(self):
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="x", node=NarRegular(contents=b"xxx")),
            ]
        )
        found = find_nar_entry(node, "x")
        assert isinstance(found, NarRegular)
        assert found.contents == b"xxx"

    async def test_find_nar_entry_missing(self):
        node = NarDirectory(entries=[])
        assert find_nar_entry(node, "missing") is None


# ═════════════════════════════════════════════════════════════════════════════
# 4. File I/O
# ═════════════════════════════════════════════════════════════════════════════


class TestNarFileIO:
    """Roundtrip through the filesystem."""

    async def test_write_and_read_path(self, tmp_path):
        path = tmp_path / "test.nar"
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="file.txt", node=NarRegular(contents=b"from file")),
            ]
        )
        write_nar_to_path(node, path)
        result = parse_nar_from_path(path)
        assert isinstance(result, NarDirectory)
        assert result.entries[0].name == "file.txt"
        assert isinstance(result.entries[0].node, NarRegular)
        assert result.entries[0].node.contents == b"from file"


# ═════════════════════════════════════════════════════════════════════════════
# 5. Streaming NAR support
# ═════════════════════════════════════════════════════════════════════════════


class TestNarForwarder:
    """Test the streaming NAR forwarder with various chunk sizes."""

    async def test_forward_whole(self):
        """Feed the entire NAR in one chunk."""
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="a.txt", node=NarRegular(contents=b"aaa")),
            ]
        )
        data = write_nar(node)
        forwarder = NarForwarder()
        tokens = list(forwarder.feed(data))
        assert forwarder.complete
        assert b"".join(tokens) == data

    async def test_forward_byte_by_byte(self):
        """Feed one byte at a time — the hardest case."""
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="a.txt", node=NarRegular(contents=b"aaa")),
            ]
        )
        data = write_nar(node)
        forwarder = NarForwarder()
        tokens: list[bytes] = []
        for byte in data:
            tokens.extend(forwarder.feed(bytes([byte])))
        assert forwarder.complete
        assert b"".join(tokens) == data

    async def test_forward_random_chunks(self):
        """Feed in random-sized chunks."""
        import random

        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="x", node=NarRegular(contents=b"x" * 1000)),
                NarDirectoryEntry(
                    name="y",
                    node=NarDirectory(
                        entries=[
                            NarDirectoryEntry(name="z", node=NarSymlink(target="/a")),
                        ]
                    ),
                ),
            ]
        )
        data = write_nar(node)
        rng = random.Random(42)
        offset = 0
        forwarder = NarForwarder()
        tokens: list[bytes] = []
        while offset < len(data):
            size = rng.randint(1, 64)
            chunk = data[offset : offset + size]
            tokens.extend(forwarder.feed(chunk))
            offset += len(chunk)
        assert forwarder.complete
        assert b"".join(tokens) == data

    async def test_forward_executable(self):
        """Forwarding preserves executable bit via token content."""
        node = NarRegular(contents=b"#!/bin/sh", executable=True)
        data = write_nar(node)
        forwarder = NarForwarder()
        tokens = list(forwarder.feed(data))
        assert forwarder.complete
        assert b"".join(tokens) == data

    async def test_forward_empty_directory(self):
        """An empty directory has just the terminator ')' token."""
        node = NarDirectory(entries=[])
        data = write_nar(node)
        forwarder = NarForwarder()
        tokens = list(forwarder.feed(data))
        assert forwarder.complete
        assert b"".join(tokens) == data


class TestForwardNar:
    """Test the convenience forward_nar function."""

    async def test_forward_nar(self):
        import io

        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="file.txt", node=NarRegular(contents=b"hello")),
            ]
        )
        data = write_nar(node)
        src = io.BytesIO(data)
        dst = io.BytesIO()
        total = forward_nar(src, dst)
        assert total == len(data)
        assert dst.getvalue() == data


class TestNarSize:
    """Test nar_size() for extracting NAR length from a larger buffer."""

    async def test_nar_size_exact(self):
        node = NarRegular(contents=b"test")
        data = write_nar(node)
        assert nar_size(data) == len(data)

    async def test_nar_size_with_trailing(self):
        node = NarRegular(contents=b"test")
        data = write_nar(node) + b"EXTRA_TRAILING_BYTES"
        assert nar_size(data) == len(write_nar(node))

    async def test_nar_size_nested(self):
        node = NarDirectory(
            entries=[
                NarDirectoryEntry(name="a", node=NarDirectory(entries=[])),
                NarDirectoryEntry(name="b", node=NarRegular(contents=b"bbb")),
            ]
        )
        data = write_nar(node)
        assert nar_size(data) == len(data)

    async def test_nar_size_incomplete(self):
        with pytest.raises(ValueError, match="Incomplete"):
            nar_size(b"nix-arc")  # too short

    async def test_nar_size_partial(self):
        """A buffer that starts with a valid NAR magic but is truncated."""
        node = NarRegular(contents=b"x")
        data = write_nar(node)
        with pytest.raises(ValueError, match="Incomplete"):
            nar_size(data[: len(data) // 2])
