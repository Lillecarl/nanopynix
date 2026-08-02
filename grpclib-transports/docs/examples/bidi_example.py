"""Bidirectional logical RPC — in-process request/response without gRPC.

Demonstrates :class:`~grpclib_transports.LogicalRpcPeer` for lightweight
in-process RPC that doesn't require protobuf or HTTP/2.

Run with::

    python docs/examples/bidi_example.py
"""

from __future__ import annotations

import asyncio

from grpclib_transports import LogicalFrame, LogicalRpcPeer, PeerClosedError


async def handler(method: str, payload: str) -> str:
    print(f"  [server] received {method!r} with {payload!r}")
    return payload.upper()


async def main() -> None:
    up: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()
    down: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()

    server = LogicalRpcPeer(
        send_frame=down.put,
        receive_frame=up.get,
        handler=handler,
    )
    client = LogicalRpcPeer(
        send_frame=up.put,
        receive_frame=down.get,
    )
    server.start()
    client.start()

    try:
        response = await client.call("echo", "hello world")
        assert response == "HELLO WORLD"
        print(f"client got: {response}")

        await client.event("notify", "side-effect")
    finally:
        await client.aclose()
        await server.aclose()

    try:
        await client.call("fail", None)
    except PeerClosedError:
        print("correctly raised PeerClosedError after close")


if __name__ == "__main__":
    asyncio.run(main())
