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
    import os
    from collections.abc import Sequence

    from anyio.abc import ByteReceiveStream, ByteSendStream


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


async def run_process(
    command: Sequence[str],
    cwd: str | os.PathLike[str] | None = None,
    stdin: bytes | None = None,
) -> CompletedProcess:
    """Run ``command``, draining both pipes concurrently, and return its output.

    ``cwd`` runs the child in that directory. A tool that finds its own
    configuration by walking up from the working directory needs this, and
    rewriting the command with explicit paths instead would no longer be the
    command that CI runs.

    ``stdin`` writes those bytes to the child and then closes the pipe, so a
    child that reads to end of file sees one. This exists to keep a shell out
    of the command: ``sh -c 'cmd < file'`` is a host program, and a host
    program cannot load under the ``LD_PRELOAD`` of the sanitizer runtime
    (``test_store_exec.py`` gives the whole failure). The write goes in the
    same task group as the two drains, and not before them, for the reason
    this module exists: a child that fills its stdout pipe stops reading its
    stdin, and a parent that writes stdin first would then never drain the
    stdout that unblocks it.
    """
    process = await anyio.open_process(list(command), cwd=cwd)
    captured: dict[str, bytes] = {"stdout": b"", "stderr": b""}

    async def drain(name: str, stream: ByteReceiveStream | None) -> None:
        if stream is None:
            return
        while True:
            try:
                captured[name] += await stream.receive()
            except anyio.EndOfStream:
                return

    async def feed(payload: bytes, stream: ByteSendStream | None) -> None:
        if stream is None:
            return
        async with stream:
            await stream.send(payload)

    async with process:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(drain, "stdout", process.stdout)
            task_group.start_soon(drain, "stderr", process.stderr)
            if stdin is not None:
                task_group.start_soon(feed, stdin, process.stdin)
        await process.wait()

    return CompletedProcess(
        returncode=process.returncode or 0,
        stdout=captured["stdout"].decode(errors="replace"),
        stderr=captured["stderr"].decode(errors="replace"),
    )
