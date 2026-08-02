"""Convenience helpers for creating tuned gRPC client channels."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from grpclib.client import Channel

from grpclib_transports.protocol import DEFAULT_TUNING, TransportTuning, make_config

if TYPE_CHECKING:
    from pathlib import Path
    from ssl import SSLContext


def connect_unix(
    path: str | Path,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    **kwargs: Any,
) -> Channel:
    """Create a :class:`grpclib.client.Channel` to a Unix-domain socket at *path*.

    The channel is configured with tuned HTTP/2 window sizes from *tuning*.
    """
    kwargs.setdefault("config", make_config(tuning))
    return Channel(path=str(path), **kwargs)


def connect_tcp(
    host: str,
    port: int,
    *,
    tuning: TransportTuning = DEFAULT_TUNING,
    ssl: SSLContext | bool | None = None,
    **kwargs: Any,
) -> Channel:
    """Create a :class:`grpclib.client.Channel` to *host*:*port* over TCP.

    The channel is configured with tuned HTTP/2 window sizes from *tuning*.
    Pass *ssl* to enable TLS.
    """
    kwargs.setdefault("config", make_config(tuning))
    return Channel(host=host, port=port, ssl=ssl, **kwargs)
