"""``run_process`` writes a child's stdin without deadlocking on it.

``tests/support/subprocess_output.py`` exists because a caller that reads one
pipe of a child and not the other hangs the moment the child fills the pipe it
is not reading. The ``stdin`` argument adds a third stream to that same trap,
and from the other direction: a parent that writes the whole stdin before it
drains stdout stops as soon as the child stops reading, and the child stops
reading as soon as its own stdout pipe is full.

No other test in the suite would notice. The one caller,
``test_store_dump_db.py``, sends about a kilobyte, and a kilobyte fits in the
64 KiB pipe buffer whatever order the streams run in. So the ordering is
correct there by accident, and the test below is what makes it correct on
purpose.

The child is ``sys.executable`` and not ``cat``, for the reason
``test_store_exec.py:36`` gives: a host program cannot load under the
``LD_PRELOAD`` of the sanitizer runtime, and the ASAN jobs run this file too.
"""

from __future__ import annotations

import sys

import anyio

from test_support.subprocess_output import run_process

# Far above the 64 KiB pipe buffer of Linux, in each direction. The margin is
# deliberate and large: the two pipes are not the only buffer between the
# parent and the child. The writer of asyncio holds another 64 KiB before it
# waits, and the child holds what it has read. A payload of a few hundred
# kilobytes fits inside the sum of those, and a test that fits inside them
# passes whatever order the parent uses.
_PAYLOAD = b"nanopynix stdin round trip\n" * 160_000

# The child must write **while** it reads, and this is the part that makes the
# test discriminate. A child that reads to end of file first and echoes
# afterwards never fills its stdout pipe while the parent is still feeding, so
# it passes whatever order the parent uses. `read1` returns what has arrived
# rather than waiting for a full block, and the flush puts it on the pipe at
# once.
_ECHO = """
import sys
while chunk := sys.stdin.buffer.read1(4096):
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
"""


async def test_run_process_feeds_stdin_larger_than_the_pipe_buffer() -> None:
    """The whole payload arrives, and the call returns.

    The measured behaviour of the wrong order is a hang, and not a wrong
    answer, so the deadline is the assertion. `tests/conftest.py` already ends
    every async test at 120 s. This one ends sooner, because the right answer
    takes under a second, and a regression that waits two minutes tells the
    reader nothing the first twenty do not.
    """
    assert len(_PAYLOAD) > 1024 * 1024, "the payload must exceed every buffer on the way, or this proves nothing"

    with anyio.fail_after(20):
        result = await run_process([sys.executable, "-c", _ECHO], stdin=_PAYLOAD)

    assert result.returncode == 0, result.describe()
    assert result.stdout.encode() == _PAYLOAD


async def test_run_process_closes_stdin_so_a_child_reaches_end_of_file() -> None:
    """A child that reads to end of file gets one, rather than waiting forever."""
    result = await run_process([sys.executable, "-c", _ECHO], stdin=b"")

    assert result.returncode == 0, result.describe()
    assert result.stdout == ""
