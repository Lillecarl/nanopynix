"""Shared protocol helpers: H2 configuration, tuning, transport base class, pump loop, and peer identity.

This is the core module.  Nearly every other module in the package imports
from here.
"""

from __future__ import annotations

import asyncio
import grp
import inspect
import os
import pwd
import re
import socket
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grpclib.config import Configuration
from grpclib.encoding.proto import ProtoCodec
from grpclib.events import _DispatchServerEvents  # pyright: ignore[reportPrivateUsage] -- grpclib internal API
from grpclib.protocol import H2Protocol
from grpclib.server import Handler as ServerHandler
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.errors import ErrorCodes
from h2.exceptions import StreamClosedError, StreamIDTooLowError
from h2.settings import SettingCodes
from hyperframe.frame import RstStreamFrame

from grpclib_transports.limits import limit_services_concurrency

if TYPE_CHECKING:
    from collections.abc import Sequence

    from grpclib._typing import IServable
    from grpclib.const import Handler
    from grpclib.encoding.base import StatusDetailsCodecBase

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "ki": 1024,
    "kib": 1024,
    "mi": 1024**2,
    "mib": 1024**2,
    "gi": 1024**3,
    "gib": 1024**3,
}


def _parse_size(value: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([a-zA-Z]*)", value.strip())
    if match is None:
        raise ValueError("expected an integer byte count or a size like 8MiB")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    multiplier = _SIZE_UNITS.get(unit)
    if multiplier is None:
        raise ValueError(f"unknown size unit {unit!r}")
    return amount * multiplier


def _env_size(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = _parse_size(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer byte count or size literal") from e
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


_h2_fast_receive_patch_installed = False


# A verbatim copy of `h2.connection.H2Connection._receive_frame` with the
# trace-logging frame reprs removed -- those reprs are formatted on every
# frame, whether or not trace logging is enabled, and they dominated the
# profile of a saturated stdio transport.
#
# Being a copy of a private method, every line that touches `self` touches an
# h2 private member, and h2 exposes no public equivalent for any of them. Each
# `pyright: ignore` below therefore says the same thing, which is why the
# reasons are short: the argument is here. `install_h2_fast_receive_patch`
# checks h2's own signature before installing this and raises if upstream has
# changed it, which is the guard that actually protects the copy.
def _receive_frame_without_trace_repr(  # pyright: ignore[reportPrivateUsage] -- a copy of an h2 private method, so the name is private too
    self: H2Connection,
    frame: Any,
) -> list[Any]:
    try:
        frames, events = self._frame_dispatch_table[frame.__class__](frame)  # pyright: ignore[reportPrivateUsage,reportUnknownMemberType,reportUnknownVariableType,reportUnknownArgumentType] -- h2 ships no annotations for this private member
    except StreamClosedError as e:
        if self._stream_is_closed_by_reset(e.stream_id):  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
            f = RstStreamFrame(e.stream_id)
            f.error_code = e.error_code
            self._prepare_for_sending([f])  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
            events = e._events  # pyright: ignore[reportPrivateUsage,reportUnknownMemberType] -- h2 ships no annotations for this private member
        else:
            raise
    except StreamIDTooLowError as e:
        if self._stream_is_closed_by_reset(e.stream_id):  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
            f = RstStreamFrame(e.stream_id)
            f.error_code = ErrorCodes.STREAM_CLOSED
            self._prepare_for_sending([f])  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
            events = []
        elif self._stream_is_closed_by_end(e.stream_id):  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
            raise StreamClosedError(e.stream_id) from e
        else:
            raise
    else:
        self._prepare_for_sending(frames)  # pyright: ignore[reportPrivateUsage,reportUnknownArgumentType] -- h2 ships no annotations for this private member
    return events  # pyright: ignore[reportUnknownVariableType] -- h2 ships no annotations for this private member


def install_h2_fast_receive_patch() -> None:
    """Patch ``h2`` to skip expensive trace-logging frame reprs.

    Called automatically at import time. Idempotent — subsequent calls are
    no-ops.  Raises :exc:`RuntimeError` if the ``h2`` internals have changed
    in a way the patch cannot handle.
    """
    global _h2_fast_receive_patch_installed

    if _h2_fast_receive_patch_installed:
        return

    params = tuple(inspect.signature(H2Connection._receive_frame).parameters)  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
    if params != ("self", "frame"):
        raise RuntimeError(f"Unsupported h2 H2Connection._receive_frame signature: {params!r}")
    H2Connection._receive_frame = _receive_frame_without_trace_repr  # pyright: ignore[reportPrivateUsage] -- h2 private member, no public equivalent
    _h2_fast_receive_patch_installed = True


install_h2_fast_receive_patch()

MAX_FRAME_SIZE = 2**24 - 1
DEFAULT_HTTP2_MAX_FRAME_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TransportTuning:
    """Buffer, window, and chunk size knobs for transport performance.

    All values are in bytes.  Create a tuned instance with :meth:`from_env`
    or read the pre-computed :data:`DEFAULT_TUNING`.

    Fields:
        buffer_size: OS pipe buffer size used when bumping subprocess pipes.
        read_chunk_size: Size of each ``read()`` call in the pump loop.
        write_high_water: High-water mark for write buffer limits.
        write_low_water: Low-water mark for write buffer limits.
        http2_stream_window_size: Per-stream HTTP/2 flow-control window.
        http2_connection_window_size: Connection-level HTTP/2 flow-control window.
        http2_max_frame_size: Maximum HTTP/2 frame size sent to the peer.
        transfer_chunk_size: Default chunk size for file/data chunk iterators.
    """

    buffer_size: int
    read_chunk_size: int
    write_high_water: int
    write_low_water: int
    http2_stream_window_size: int
    http2_connection_window_size: int
    http2_max_frame_size: int
    transfer_chunk_size: int

    @classmethod
    def from_env(cls) -> TransportTuning:
        buffer_size = _env_size("GRPCLAB_BUFFER_SIZE", 8 * 1024 * 1024)
        transfer_chunk_size = _env_size(
            "GRPCLAB_TRANSFER_CHUNK_SIZE",
            256 * 1024,
        )
        stream_window_size = _env_size(
            "GRPCLAB_HTTP2_STREAM_WINDOW_SIZE",
            max(16 * 1024 * 1024, buffer_size * 2),
        )
        connection_window_size = _env_size(
            "GRPCLAB_HTTP2_CONNECTION_WINDOW_SIZE",
            max(64 * 1024 * 1024, stream_window_size * 4),
        )
        max_frame_size = _env_size(
            "GRPCLAB_HTTP2_MAX_FRAME_SIZE",
            min(DEFAULT_HTTP2_MAX_FRAME_SIZE, buffer_size, MAX_FRAME_SIZE),
        )
        if max_frame_size > MAX_FRAME_SIZE:
            raise ValueError("GRPCLAB_HTTP2_MAX_FRAME_SIZE exceeds HTTP/2 maximum frame size")
        return cls(
            buffer_size=buffer_size,
            read_chunk_size=buffer_size,
            write_high_water=buffer_size,
            write_low_water=buffer_size // 2,
            http2_stream_window_size=stream_window_size,
            http2_connection_window_size=connection_window_size,
            http2_max_frame_size=max_frame_size,
            transfer_chunk_size=transfer_chunk_size,
        )


DEFAULT_TUNING = TransportTuning.from_env()
"""Pre-computed :class:`TransportTuning` from environment variables.

Overridable via ``GRPCLAB_BUFFER_SIZE``, ``GRPCLAB_TRANSFER_CHUNK_SIZE``,
``GRPCLAB_HTTP2_STREAM_WINDOW_SIZE``, ``GRPCLAB_HTTP2_CONNECTION_WINDOW_SIZE``,
and ``GRPCLAB_HTTP2_MAX_FRAME_SIZE`` (all support suffixes like ``8MiB``).
"""


@dataclass(frozen=True)
class PeerIdentity:
    """Identity information about the remote peer of a transport.

    Fields:
        transport: Transport type (``"stdio"``, ``"ssh"``, ``"unix"``, ``"unknown"``).
        username: OS username of the peer, if available.
        uid: Unix user ID of the peer.
        gid: Unix group ID of the peer.
        pid: Process ID of the peer.
        group_ids: Supplementary group IDs.
        group_names: Supplementary group names (resolved from *group_ids*).
    """

    transport: str
    username: str | None
    uid: int | None = None
    gid: int | None = None
    pid: int | None = None
    group_ids: tuple[int, ...] | None = None
    group_names: tuple[str, ...] | None = None


def _username_for_uid(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _group_names(group_ids: Sequence[int]) -> tuple[str, ...]:
    names: list[str] = []
    for group_id in group_ids:
        try:
            names.append(grp.getgrgid(group_id).gr_name)
        except KeyError:
            names.append(str(group_id))
    return tuple(names)


def _groups_for_user(username: str, gid: int) -> tuple[int, ...] | None:
    try:
        return tuple(os.getgrouplist(username, gid))
    except OSError:
        return None


def local_process_identity(*, transport: str) -> PeerIdentity:
    """Build a :class:`PeerIdentity` for the current process."""
    uid = os.geteuid()
    gid = os.getegid()
    username = _username_for_uid(uid)
    group_ids = tuple(os.getgroups())
    return PeerIdentity(
        transport=transport,
        username=username,
        uid=uid,
        gid=gid,
        pid=os.getpid(),
        group_ids=group_ids,
        group_names=_group_names(group_ids),
    )


def _unix_socket_identity(sock: socket.socket) -> PeerIdentity | None:
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
    except OSError:
        return None
    pid, uid, gid = struct.unpack("3i", raw)
    username = _username_for_uid(uid)
    group_ids = _groups_for_user(username, gid) if username is not None else None
    return PeerIdentity(
        transport="unix",
        username=username,
        uid=uid,
        gid=gid,
        pid=pid,
        group_ids=group_ids,
        group_names=_group_names(group_ids) if group_ids is not None else None,
    )


def peer_identity_from_transport(transport: Any) -> PeerIdentity:
    """Extract :class:`PeerIdentity` from an asyncio transport.

    Checks for a stored ``peer_identity`` extra, an SSH username, or a
    Unix-domain socket with ``SO_PEERCRED``.
    """
    identity = transport.get_extra_info("peer_identity")
    if isinstance(identity, PeerIdentity):
        return identity

    ssh_username = transport.get_extra_info("username")
    if isinstance(ssh_username, str):
        return PeerIdentity(transport="ssh", username=ssh_username)

    sock = transport.get_extra_info("socket")
    if isinstance(sock, socket.socket) and sock.family == socket.AF_UNIX:
        identity = _unix_socket_identity(sock)
        if identity is not None:
            return identity

    return PeerIdentity(transport="unknown", username=None)


def peer_identity_from_stream(stream: Any) -> PeerIdentity:
    """Extract :class:`PeerIdentity` from a gRPC stream's underlying transport."""
    transport = stream.peer._transport
    return peer_identity_from_transport(transport)


class BaseCustomTransport(asyncio.Transport):
    """Base class for custom asyncio transports that wrap a reader/writer pair.

    Concrete transports (StdioTransport, SshTransport) override:
    - ``write()``         — forward data to the underlying writer
    - ``get_extra_info()`` — delegate to the appropriate underlying object
    - ``get_write_buffer_size()`` — introspect the write buffer (for flow control)
    - ``abort()``         — hard reset (transport-specific)
    - ``can_write_eof()`` / ``write_eof()`` — EOF support (transport-specific)

    Flow-control bridging (pause_writing/resume_writing forwarding to the
    H2Protocol) is handled by each concrete transport's own mechanism.
    """

    def __init__(self) -> None:
        super().__init__()
        self._protocol: asyncio.BaseProtocol | None = None
        self._closing = False

    def is_closing(self) -> bool:
        return self._closing

    def get_protocol(self) -> asyncio.BaseProtocol:
        protocol = self._protocol
        if protocol is None:
            raise RuntimeError("protocol has not been set yet")
        return protocol

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        self._protocol = protocol

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


def pause_h2_protocol(protocol: asyncio.BaseProtocol | None) -> None:
    """Pause writing on an H2 protocol, if set."""
    if protocol is not None:
        protocol.pause_writing()


def resume_h2_protocol(protocol: asyncio.BaseProtocol | None) -> None:
    """Resume writing on an H2 protocol, if set."""
    if protocol is not None:
        protocol.resume_writing()


async def pump(
    protocol: H2Protocol,
    reader: Any,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
) -> None:
    """Read from a byte stream and feed data into an H2 protocol.

    Blocks in a loop calling ``reader.read()``.  On EOF or error, calls
    ``protocol.connection_lost()``.
    """
    exc: BaseException | None = None
    try:
        while True:
            data = await reader.read(tuning.read_chunk_size)
            if not data:
                break
            protocol.data_received(data)
    except (ConnectionError, OSError) as e:
        exc = e
    finally:
        protocol.connection_lost(exc)


async def serve_h2(
    handlers: Sequence[IServable],
    reader: Any,
    transport: Any,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    max_concurrency: int | None = None,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> None:
    """Build a server protocol, wire it to a transport, and pump frames."""
    mapping = build_mapping(
        limit_services_concurrency(
            handlers,
            max_concurrency=max_concurrency,
        )
    )
    protocol = make_server_protocol(
        mapping,
        tuning=tuning,
        status_details_codec=status_details_codec,
    )
    init_h2_transport(protocol, transport, tuning=tuning)
    await pump(protocol, reader, tuning=tuning)


def make_h2_config(*, client_side: bool) -> H2Configuration:
    """Build an ``h2`` configuration with strict validation off.

    Disables inbound/outbound header validation and normalization so
    protobuf-based headers pass through unchanged.
    """
    return H2Configuration(
        client_side=client_side,
        header_encoding="ascii",
        validate_inbound_headers=False,
        validate_outbound_headers=False,
        normalize_inbound_headers=False,
        normalize_outbound_headers=False,
    )


def make_config(tuning: TransportTuning = DEFAULT_TUNING) -> Configuration:
    """Build a grpclib :class:`Configuration` with tuned window sizes."""
    return Configuration(
        http2_connection_window_size=tuning.http2_connection_window_size,
        http2_stream_window_size=tuning.http2_stream_window_size,
    )


def make_server_protocol(
    mapping: dict[str, Handler],
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    status_details_codec: StatusDetailsCodecBase | None = None,
) -> H2Protocol:
    """Build a server-side H2 protocol with the given handler mapping.

    ``status_details_codec`` is what lets a handler's ``GRPCError.details``
    reach the client: grpclib encodes it into the ``grpc-status-details-bin``
    trailer only when a codec is configured, and drops it silently otherwise.
    The client's channel must be given the same codec to decode it.
    """
    config = make_config(tuning).__for_server__()
    h2_config = make_h2_config(client_side=False)
    handler = ServerHandler(mapping, ProtoCodec(), status_details_codec, _DispatchServerEvents())  # pyright: ignore[reportPrivateUsage] -- grpclib internal API
    return H2Protocol(handler, config, h2_config)


def init_h2_transport(
    protocol: H2Protocol,
    transport: Any,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
) -> None:
    """Wire an H2 protocol to a transport and advertise a tuned max frame size."""
    transport.set_protocol(protocol)
    protocol.connection_made(transport)
    protocol.connection._connection.update_settings(  # pyright: ignore[reportPrivateUsage] -- h2 internal attribute
        {
            SettingCodes.MAX_FRAME_SIZE: tuning.http2_max_frame_size,
        }
    )
    protocol.connection.flush()


def build_mapping(handlers: Sequence[IServable]) -> dict[str, Handler]:
    """Merge ``__mapping__()`` from a sequence of servable handlers into one dict."""
    mapping: dict[str, Handler] = {}
    for h in handlers:
        mapping.update(h.__mapping__())
    return mapping


def signal_stop(stop: asyncio.Future[None]) -> None:
    """Signal a stop future, guarding against duplicate completion."""
    if not stop.done():
        stop.set_result(None)
