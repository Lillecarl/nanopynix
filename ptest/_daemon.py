"""Spawn one ``nix daemon`` over a store root, for the shared-daemon experiment.

A small reimplementation of what ``tests/support/nix_environment.py`` does, kept
separate because the prototype's whole point is to re-derive the setup rather
than inherit it. The options are copied deliberately, and for the reason stated
there: a daemon left to read the host's ``nix.conf`` disagrees with a
``load_config=False`` client about experimental features, which produced a
CI-only failure once already.

Unlike that harness this one does not install ``PR_SET_PDEATHSIG`` -- anyio's
``open_process`` has no ``preexec_fn`` -- so the daemon is left in pytest's own
process group and dies with it normally. A SIGKILLed pytest would strand it.
That is acceptable for a prototype and would not be for the real suite.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

DEFAULT_EXPERIMENTAL_FEATURES = ("nix-command", "flakes")
SOCKET_WAIT_SECONDS = 10.0


@contextlib.asynccontextmanager
async def daemon_for(root: Path) -> AsyncIterator[str]:
    """Run a daemon over *root*, yielding the store URI clients should use."""
    socket_path = root / "nix" / "var" / "nix" / "daemon-socket" / "socket"
    await anyio.Path(socket_path.parent).mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "NIX_DAEMON_SOCKET_PATH": str(socket_path)}
    process = await anyio.open_process(
        [
            "nix",
            "daemon",
            "--store",
            f"local://{root}",
            "--option",
            "build-users-group",
            "",
            "--option",
            "require-drop-supplementary-groups",
            "false",
            # Pinned rather than inherited: a client with load_config=False and
            # a daemon reading the host's nix.conf disagree about experimental
            # features, and a command-line --option outranks any config file.
            "--option",
            "experimental-features",
            " ".join(DEFAULT_EXPERIMENTAL_FEATURES),
        ],
        env=env,
        stdout=None,
        stderr=None,
    )
    try:
        with anyio.fail_after(SOCKET_WAIT_SECONDS):
            while not await anyio.Path(socket_path).is_socket():
                if process.returncode is not None:
                    raise RuntimeError(f"nix daemon exited with status {process.returncode}")
                await anyio.sleep(0.05)

        # The socket accepts connections before the daemon lays out the store
        # directory; force that lazy init before any client can race it.
        uri = f"unix://{socket_path}?root={root}"
        warmup = await anyio.run_process(
            ["nix", "store", "info", "--store", uri],
            check=False,
            env=env,
        )
        if warmup.returncode != 0:
            raise RuntimeError(
                f"daemon warmup failed ({warmup.returncode}): {warmup.stderr.decode(errors='replace')[-400:]}"
            )
        yield uri
    finally:
        process.terminate()
        with anyio.move_on_after(5, shield=True):
            await process.wait()
