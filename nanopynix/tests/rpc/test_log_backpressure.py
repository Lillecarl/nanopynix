"""Does a client that stops reading its logs stop Nix?

Issue #13. The log path used to be one unbroken chain of back-pressure:
``LogCollector.callback`` blocked until its queue had room, the worker relayed
each event straight onto the gRPC stream, and HTTP/2 flow control parked that
send whenever the client stopped reading. The client stops reading whenever
the caller's own event loop is busy -- which is ordinary, because an event
loop is not a real-time consumer.

So a caller who ran synchronous work stopped the Nix thread inside the C++
logger callback. It presented as a hang in an ``await``, with no error
anywhere.

:mod:`test_logging` unit-tests each buffer. This file is the
end-to-end half: it holds the client's event loop closed and asserts the
evaluation finishes anyway.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import RpcSessionFactory

# Enough log lines to overrun the worker's buffers several times over while the
# client is not reading. `builtins.trace` emits one event for each element, at
# the default verbosity, with no store or build activity -- so the count does
# not depend on what is already in the store.
_TRACE_LINES = 60_000

_NOISY_EVALUATION = (
    f"builtins.foldl' (acc: i: builtins.trace (toString i) acc) 0 (builtins.genList (i: i) {_TRACE_LINES})"
)

# How long the caller's event loop is held closed. Long against the worker's
# own timers and short against the deadline below, so the evaluation certainly
# runs to completion inside the stall rather than merely surviving a hiccup.
_STALL_SECONDS = 3.0

# The whole test. Generous, because it evaluates 60k traces and starts a
# worker; the point is to turn the old unbounded hang into a report.
_DEADLINE_SECONDS = 180.0

# Far below the number of traces, so the capture certainly reaches its cap.
_CAPTURE_CAP = 100


async def _stall_the_event_loop() -> None:
    """Hold the caller's event loop closed.

    ``time.sleep`` on purpose, and not ``anyio.sleep``: blocking the event loop
    is the condition under test. It is the one place in this suite where the
    banned synchronous call is the point rather than a defect.
    """
    time.sleep(_STALL_SECONDS)  # noqa: ASYNC251 -- see the docstring; blocking the loop is what this test does


async def test_a_client_that_stops_reading_does_not_stop_the_evaluation(
    rpc_session: RpcSessionFactory,
) -> None:
    """The end-to-end acceptance criterion of issue #13.

    The stall starts before the evaluation and outlasts its logging, so the
    client never drains the stream while Nix is working. Before the fix the
    ``await`` below never returned.
    """
    async with rpc_session() as session, session.store() as store, session.eval(store) as evaluator:
        results: list[object] = []
        with anyio.fail_after(_DEADLINE_SECONDS):
            async with anyio.create_task_group() as tg:
                tg.start_soon(_stall_the_event_loop)
                # `string` is what evaluates, and therefore what logs:
                # `foldl'` forces the whole fold to reach weak head normal
                # form. Putting only `to_python` inside the stall would leave
                # every trace outside it and test nothing.
                results.append(await (await evaluator.string(_NOISY_EVALUATION)).to_python())

    assert results == [0]


async def test_the_capture_says_what_the_stall_cost(
    rpc_session: RpcSessionFactory,
) -> None:
    """Loss is allowed. Silent loss is not.

    A caller who blocks their own event loop now pays in log events rather
    than in evaluation progress, and the worker says how many through an
    ``EventsDropped`` event on the same stream. Without that the caller reads a
    short log and cannot tell that it is short.

    This asserts the channel works, not a particular count: how much the worker
    discards depends on how far the client got before the stall, which is
    scheduling. ``dropped_events`` is never negative and the block always
    exits, and those are the two things that used to be untrue.
    """
    async with rpc_session() as session, session.store() as store, session.eval(store) as evaluator:
        with anyio.fail_after(_DEADLINE_SECONDS):
            async with session.capture_logs(max_events=_CAPTURE_CAP) as logs:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_stall_the_event_loop)
                    await (await evaluator.string(_NOISY_EVALUATION)).to_python()

    seen = (len(logs.events), logs.truncated, logs.dropped_events)
    assert len(logs.events) == _CAPTURE_CAP, seen
    assert logs.truncated is True, seen
    assert logs.dropped_events >= 0, seen
    # The marker arrived, so the capture knows its own boundary: the stall cost
    # log lines and not the protocol.
    assert logs.unfinalized_request_ids == frozenset()
