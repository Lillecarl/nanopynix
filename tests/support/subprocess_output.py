"""Run a subprocess to completion without deadlocking on its pipes.

`anyio.open_process` defaults *both* `stdout` and `stderr` to `PIPE` (unlike
asyncio, which inherits). A caller that reads only one of them hangs the moment
the child writes more than the other pipe's buffer -- 64 KiB on Linux -- because
the child blocks in `write()` and the parent blocks in `wait()`. Nothing times
out; the test simply stops.

This was not hypothetical. Three call sites in this suite had the one-stream
loop, and wiring up beartype turned the latent case into a real hang: an
instrumented Python child emitted ~243 KB of `BeartypeClawDecorWarning` to
stderr -- almost four times the buffer -- and every one of those children
stopped dead. Making `nanopynix.protocols` runtime-checkable has since cut that
to ~18 KB, which is under the buffer again, so do not read the current number
as the margin of safety: it is one noisy dependency away from being over it,
and the pipes have to be drained either way.

Draining the streams one after the other does not fix it -- stdout-then-stderr
deadlocks identically when stderr is the stream that fills first. They have to
be read concurrently, which is what the task group below is for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import anyio

if TYPE_CHECKING:
    from collections.abc import Sequence

    from anyio.abc import ByteReceiveStream


class CompletedProcess(NamedTuple):
    """What the child exited with, and everything it wrote."""

    returncode: int
    stdout: str
    stderr: str

    def describe(self) -> str:
        """A failure message worth reading, for use in assertions.

        Includes stderr because that is where a child's traceback goes, and a
        bare "exited 1" tells whoever hits it nothing at all.
        """
        return f"exited {self.returncode}\n--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"


async def run_process(command: Sequence[str]) -> CompletedProcess:
    """Run ``command``, draining both pipes concurrently, and return its output."""
    process = await anyio.open_process(list(command))
    captured: dict[str, bytes] = {"stdout": b"", "stderr": b""}

    async def drain(name: str, stream: ByteReceiveStream | None) -> None:
        if stream is None:
            return
        while True:
            try:
                captured[name] += await stream.receive()
            except anyio.EndOfStream:
                return

    async with process:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(drain, "stdout", process.stdout)
            task_group.start_soon(drain, "stderr", process.stderr)
        await process.wait()

    return CompletedProcess(
        returncode=process.returncode or 0,
        stdout=captured["stdout"].decode(errors="replace"),
        stderr=captured["stderr"].decode(errors="replace"),
    )
