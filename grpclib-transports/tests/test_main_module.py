"""The forkserver child must not run the program's ``__main__`` again.

``multiprocessing`` re-executes ``__main__`` in the child so that a payload
defined there can be unpickled. This transport pickles module-level objects
only, so the step buys nothing and costs whatever the top level of the host
program does. See :func:`main_module_not_reexecuted`, and nanopynix issue #97.
"""

from __future__ import annotations

import contextlib
import sys
import textwrap
import types
from typing import TYPE_CHECKING

import anyio
import pytest
from grpclib_transports.multiprocessing import main_module_not_reexecuted

if TYPE_CHECKING:
    from pathlib import Path

#: The worker payload, in a module the child can import.
#:
#: It is a separate file because the guard is exactly what stops ``__main__``
#: from being importable in the child. A factory defined in the script below
#: would pickle as ``__main__._worker_services`` and then fail to resolve --
#: which is the caveat :func:`main_module_not_reexecuted` documents, and which
#: the first draft of this test walked straight into.
_WORKER_PAYLOAD = textwrap.dedent(
    """
    from grpclib_transports.example.server import WorkerGreeter


    def worker_services():
        return [WorkerGreeter()]
    """
)

#: A ``__main__`` that refuses to be imported, which is what
#: ``ansible-playbook`` is. ``ansible.cli`` reads ``sys.stdout`` while it is
#: imported, finds ``None`` there in the child, and calls ``sys.exit(5)``.
#: This script has the same shape and none of Ansible.
_HOSTILE_MAIN = textwrap.dedent(
    """
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    if __name__ != "__main__":
        # The forkserver child runs this file under "__mp_main__".
        raise SystemExit(5)

    import contextlib

    import anyio
    import greeter.greeter.common as common_pb2
    import greeter.greeter.worker as worker_grpc
    import grpclib_transports.multiprocessing as transport
    from worker_payload import worker_services


    async def main():
        async with transport.multiprocessing_worker(worker_services, preload=["greeter"]) as channel:
            stub = worker_grpc.GreeterWorkerStub(channel)
            reply = await stub.say_hello(common_pb2.HelloRequest(name="Worker"))
            print(reply.message, flush=True)


    if "--without-the-guard" in sys.argv:
        # The toggle. `multiprocessing_worker` reads this name from the module
        # globals when it spawns, so replacing it here reaches the call.
        transport.main_module_not_reexecuted = contextlib.nullcontext

    anyio.run(main)
    """
)

_SCRIPT_TIMEOUT_SECONDS = 120.0


async def _run_hostile_main(tmp_path: Path, *args: str) -> tuple[int, str]:
    """Run the script above in its own interpreter, and report what happened."""
    script = tmp_path / "hostile_main.py"
    script.write_text(_HOSTILE_MAIN)
    (tmp_path / "worker_payload.py").write_text(_WORKER_PAYLOAD)
    with anyio.fail_after(_SCRIPT_TIMEOUT_SECONDS):
        result = await anyio.run_process([sys.executable, str(script), *args], check=False)
    return result.returncode, result.stdout.decode() + result.stderr.decode()


async def test_a_worker_starts_under_a_main_that_refuses_to_be_imported(tmp_path: Path) -> None:
    returncode, output = await _run_hostile_main(tmp_path)

    assert "Hello, Worker!" in output, output
    assert returncode == 0, output


async def test_the_same_main_kills_the_worker_without_the_guard(tmp_path: Path) -> None:
    """The toggle that proves the test above tests something.

    Without the guard the child runs the top level of the script, raises
    ``SystemExit`` there, and dies before it serves anything. The parent sees
    a closed pipe. This is the failure issue #97 reports.
    """
    returncode, output = await _run_hostile_main(tmp_path, "--without-the-guard")

    assert "Hello, Worker!" not in output, output
    assert returncode != 0, output


def test_the_guard_removes_the_path_and_puts_it_back() -> None:
    main = sys.modules["__main__"]
    if getattr(main, "__file__", None) is None:
        pytest.skip("this interpreter's __main__ has no __file__, so there is nothing to hide")
    before = main.__file__

    with main_module_not_reexecuted():
        assert not hasattr(main, "__file__")

    assert main.__file__ == before


def test_the_guard_puts_the_path_back_after_a_failure() -> None:
    """``Process.start()`` can raise, and the program continues after it."""
    main = sys.modules["__main__"]
    if getattr(main, "__file__", None) is None:
        pytest.skip("this interpreter's __main__ has no __file__, so there is nothing to hide")
    before = main.__file__

    with contextlib.suppress(RuntimeError), main_module_not_reexecuted():
        raise RuntimeError("the spawn failed")

    assert main.__file__ == before


def test_the_guard_accepts_a_main_that_has_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interactive interpreter has no ``__file__``, and must not raise here."""
    module = types.ModuleType("__main__")
    monkeypatch.setitem(sys.modules, "__main__", module)

    with main_module_not_reexecuted():
        assert not hasattr(module, "__file__")

    assert not hasattr(module, "__file__")
