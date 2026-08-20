"""Does a completion give up on a flake input that accepts and then goes silent?

Issue #231 opened on this shape. `pynix._attr_completion` wraps every
completion in `anyio.fail_after`, and that bounds the task that waits, not the
fetch. A `git+http:` input is fetched by `git`, which Nix runs as a child
process, so nothing in the Python layer holds a handle that could stop it.

**Two settings close it, and neither is a cancel.** `_completion_settings`
cuts what curl waits for, and `_GIT_STALL_VARIABLES` cuts what `git` waits
for. `git` reads none of Nix's settings; the two variables here are its own,
and Nix passes its environment to the child.

Measured while this file was written, against the fixture below: at git's
defaults the completion outlasted 120 s and had to be killed, and with the
variables it answered nothing after 4.6 s and left no `git` process behind.

**The server accepts the connection and then writes nothing, which is the
whole point.** A refused connection is bounded by `connect-timeout` already,
and a routed-nowhere address is bounded by the operating system. Neither
reaches the case this file is for. `127.0.0.1` also means no network is
needed, so the gate can run this in a sandbox.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from typing import TYPE_CHECKING

import anyio
import pytest

from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: How long the completion may take before this file calls it unbounded.
#:
#: **Generous on purpose.** The measurement is 4.6 s and the budget is 5.0 s,
#: but a loaded CI runner is slower than this machine and a tight bound would
#: fail for the wrong reason. What the case has to separate is "bounded" from
#: "outlasts two minutes", and this does that with room to spare.
UNBOUNDED_AFTER_SECONDS = 45.0

_COMPLETE_SCRIPT = """
import json
import sys

from pynix._attr_completion import complete_flake

print(json.dumps(complete_flake(sys.argv[1] + "#top", "base")))
"""


@pytest.fixture
def stalled_server() -> Iterator[int]:
    """A socket that accepts every connection and never writes a byte."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []

    def accept_forever() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            held.append(connection)

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        for connection in held:
            connection.close()


@pytest.fixture
def flake_with_a_stalled_input(tmp_path: Path, stalled_server: int) -> Path:
    """A flake whose one input names the server that never answers."""
    directory = tmp_path / "stalled-flake"
    directory.mkdir()
    (directory / "flake.nix").write_text(
        "{\n"
        f'  inputs.stalled.url = "git+http://127.0.0.1:{stalled_server}/x.git";\n'
        '  outputs = { self, stalled }: { topone = "t"; };\n'
        "}\n",
        encoding="utf-8",
    )
    return directory


async def test_a_completion_gives_up_on_an_input_that_never_answers(
    flake_with_a_stalled_input: Path,
) -> None:
    """The three things issue #231 asks for, in one case.

    It answers within a bound, the process exits, and no `git` process is left
    running against the server afterwards.
    """
    started = time.monotonic()
    # **The bound is here, and not only in the assertion below.** A regression
    # does not make this case slow, it makes it never end: `git` waits on a
    # socket that no one will write to. Without this the case would hang, and
    # a gate that hangs is worse than one that fails -- the packaged runner
    # that CI uses carries no pytest-agent, so nothing else would stop it.
    try:
        with anyio.fail_after(UNBOUNDED_AFTER_SECONDS):
            result = await run_process([sys.executable, "-c", _COMPLETE_SCRIPT, str(flake_with_a_stalled_input)])
    except TimeoutError:
        pytest.fail(f"the completion did not end within {UNBOUNDED_AFTER_SECONDS} s, so nothing bounded the fetch")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.describe()
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []
    assert elapsed < UNBOUNDED_AFTER_SECONDS
