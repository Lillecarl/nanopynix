from __future__ import annotations

import asyncio
import gc

from grpclib_transports.bidi import LogicalFrame, LogicalRpcPeer, RemoteCallError


async def test_logical_rpc_peer_round_trip() -> None:
    a_to_b: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()
    b_to_a: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()

    async def send_a(frame: LogicalFrame) -> None:
        await a_to_b.put(frame)

    async def receive_a() -> LogicalFrame | None:
        return await b_to_a.get()

    async def send_b(frame: LogicalFrame) -> None:
        await b_to_a.put(frame)

    async def receive_b() -> LogicalFrame | None:
        return await a_to_b.get()

    async def handle_b(method: str, payload: object) -> object:
        return {"method": method, "payload": payload}

    peer_a = LogicalRpcPeer(send_frame=send_a, receive_frame=receive_a)
    peer_b = LogicalRpcPeer(
        send_frame=send_b,
        receive_frame=receive_b,
        handler=handle_b,
    )
    try:
        peer_b.start()
        response = await peer_a.call("echo", {"value": 1})
        assert response == {"method": "echo", "payload": {"value": 1}}
    finally:
        await peer_a.aclose()
        await peer_b.aclose()


async def test_aclose_reaps_a_reader_that_already_died() -> None:
    """A transport that breaks before aclose must not leave a task exception.

    The reader task leaves ``_tasks`` as soon as it ends, so a peer that went
    away first -- a killed process, a broken pipe -- takes its reader with it
    and aclose finds nothing to reap. The exception the reader carried then
    reappears later as "Task exception was never retrieved", in whatever code
    is running when the garbage collector notices.
    """
    collected: list[str] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: collected.append(str(context.get("message"))),
    )
    failed = asyncio.Event()

    async def receive() -> LogicalFrame | None:
        failed.set()
        raise ConnectionResetError("connection lost")

    async def send(_frame: LogicalFrame) -> None:
        return None

    peer = LogicalRpcPeer(send_frame=send, receive_frame=receive)
    peer.start()
    await failed.wait()
    # No `reader.exception()` anywhere in this test, and no local name for the
    # task: reading the exception *is* retrieving it, which is the very thing
    # the test asks aclose to do.
    await asyncio.sleep(0)

    await peer.aclose()

    del peer
    gc.collect()
    await asyncio.sleep(0)
    assert collected == [], f"aclose left an unretrieved task exception: {collected}"


async def test_logical_rpc_peer_reports_remote_errors() -> None:
    a_to_b: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()
    b_to_a: asyncio.Queue[LogicalFrame | None] = asyncio.Queue()

    async def handle_b(_method: str, _payload: object) -> object:
        raise ValueError("boom")

    peer_a = LogicalRpcPeer(
        send_frame=a_to_b.put,
        receive_frame=b_to_a.get,
    )
    peer_b = LogicalRpcPeer(
        send_frame=b_to_a.put,
        receive_frame=a_to_b.get,
        handler=handle_b,
    )
    try:
        peer_b.start()
        try:
            await peer_a.call("fail")
        except RemoteCallError as e:
            assert str(e) == "boom"
        else:
            raise AssertionError("expected RemoteCallError")
    finally:
        await peer_a.aclose()
        await peer_b.aclose()
