"""The fast-receive patch states what it reads out of ``h2``.

``_receive_frame_without_trace_repr`` is a copy of an h2 private method, so
every line of it touches a private member. The signature check alone covered
one name of five. A release that renames one of the others gives an
``AttributeError`` from inside the frame pump of a live connection, which
reaches the caller as ``WorkerDiedError: Protocol error`` and names neither h2
nor this patch.
"""

from __future__ import annotations

import inspect

import pytest
from grpclib_transports import protocol
from h2.connection import H2Connection
from hyperframe.frame import PingFrame

_MEMBERS: tuple[str, ...] = protocol._H2_PRIVATE_MEMBERS  # pyright: ignore[reportPrivateUsage] -- the module under test
_COPY_SOURCE = inspect.getsource(protocol._receive_frame_without_trace_repr)  # pyright: ignore[reportPrivateUsage] -- the function under test


def test_every_named_member_is_really_there() -> None:
    """The guard is only worth its line while the names are right.

    A typo in the tuple makes the patch refuse on a good h2, and the whole
    library then fails to import. That is worse than the hole it closes.
    """
    for name in _MEMBERS:
        assert hasattr(H2Connection, name), name


def test_the_patch_reads_no_class_member_the_tuple_leaves_out() -> None:
    """The tuple has to follow the copy, and nothing else makes it.

    ``_frame_dispatch_table`` is the one deliberate omission:
    ``H2Connection`` sets it in ``__init__``, so the class cannot answer for
    it. See nanopynix issue #299 for the CI job where an instance did not have
    it either.
    """
    read = {
        name
        for name in dir(H2Connection)
        if name.startswith("_") and not name.startswith("__") and f"self.{name}" in _COPY_SOURCE
    }

    assert read - {"_frame_dispatch_table"} == set(_MEMBERS)


def test_a_connection_without_the_table_says_what_it_is() -> None:
    """The one place the copy departs from h2, and issue #299 is why.

    A bare ``AttributeError`` here reaches the caller as ``WorkerDiedError:
    Protocol error``, which names no object. The replacement names the class,
    its bases and the attributes the instance does carry.
    """
    conn = H2Connection()
    del conn._frame_dispatch_table  # pyright: ignore[reportPrivateUsage] -- reproducing the shape issue #299 reports

    # The patched method directly, because `receive_data` reaches it only once
    # a whole frame has arrived, and the shape under test is the lookup.
    with pytest.raises(RuntimeError, match="has no _frame_dispatch_table") as raised:
        conn._receive_frame(PingFrame(0))  # pyright: ignore[reportPrivateUsage] -- the patched method is the subject

    assert "H2Connection" in str(raised.value)
    assert "incoming_buffer" in str(raised.value)
    # The discriminator of issue #299: this instance really did finish
    # `__init__`, so the report says so and the missing table was removed
    # afterwards. A connection that never finished reports `init_complete=False`.
    assert "init_complete=True" in str(raised.value)


def test_a_finished_connection_carries_the_init_mark() -> None:
    """Without the mark, `init_complete` reads False for every connection and
    the report cannot tell a partial object from a stripped one."""
    assert getattr(H2Connection(), protocol._H2_INIT_COMPLETE, False) is True  # pyright: ignore[reportPrivateUsage] -- the module under test


def test_a_renamed_member_refuses_instead_of_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "_h2_fast_receive_patch_installed", False)
    monkeypatch.setattr(protocol, "_H2_PRIVATE_MEMBERS", ("_prepare_for_sending", "_gone_upstream"))

    with pytest.raises(RuntimeError, match="_gone_upstream"):
        protocol.install_h2_fast_receive_patch()


class _Reader:
    """Hands out one chunk per ``read``, then EOF."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _ClosedConnection:
    def is_closing(self) -> bool:
        return True


class _OpenConnection:
    def is_closing(self) -> bool:
        return False


class _RecordingProtocol:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.fed: list[bytes] = []
        self.lost_with: BaseException | str | None = "not called"

    def data_received(self, data: bytes) -> None:
        self.fed.append(data)

    def connection_lost(self, exc: BaseException | None) -> None:
        self.lost_with = exc


async def test_the_pump_stops_once_the_connection_is_closing() -> None:
    """Issue #299. `grpclib.protocol.Connection.close` deletes the
    `_frame_dispatch_table` of its `H2Connection`, so a frame delivered after
    the close reaches a connection that cannot dispatch it. Under asyncio a
    closed transport delivers nothing; this pump has to impose that itself."""
    fake = _RecordingProtocol(_ClosedConnection())

    await protocol.pump(fake, _Reader(b"late bytes"))  # pyright: ignore[reportArgumentType] -- a double, and `pump` takes only these three members

    assert fake.fed == []
    assert fake.lost_with is None


async def test_the_pump_feeds_an_open_connection() -> None:
    """The control. Without it the test above passes on a pump that feeds
    nothing at all."""
    fake = _RecordingProtocol(_OpenConnection())

    await protocol.pump(fake, _Reader(b"one", b"two"))  # pyright: ignore[reportArgumentType] -- a double, and `pump` takes only these three members

    assert fake.fed == [b"one", b"two"]
    assert fake.lost_with is None


async def test_the_pump_feeds_a_protocol_with_no_connection_yet() -> None:
    """`H2Protocol.connection` is an annotation until `connection_made` runs,
    so the guard must not read a closed connection into its absence."""
    fake = _RecordingProtocol(_OpenConnection())
    del fake.connection

    await protocol.pump(fake, _Reader(b"early"))  # pyright: ignore[reportArgumentType] -- a double, and `pump` takes only these three members

    assert fake.fed == [b"early"]
