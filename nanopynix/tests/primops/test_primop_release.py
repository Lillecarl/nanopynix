"""A registered primop must not keep a closed evaluator, or its store, alive.

The huggorm engine gives each evaluator a bridge to every registered primop,
and the evaluator holds the bridge. A bridge that held its evaluator back made
a cycle that only the cyclic collector frees. Until it ran, each store stayed
open with its database, and the lane crossed 1024 descriptors: prompt_toolkit's
``select()`` then refused its pipe on every pass of the event loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

import nanopynix

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment


async def _open_descriptors() -> int:
    return len([fd async for fd in anyio.Path("/proc/self/fd").iterdir()])


async def test_a_closed_evaluator_with_a_primop_frees_its_store(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path
) -> None:
    nanopynix.register_primop("test_release_probe", 1, lambda x: x)  # type: ignore[reportUnknownLambdaType] -- primop callbacks receive Any from Nix
    uri = f"local://?root={tmp_path}"

    async def one_session() -> None:
        async with (
            shared_nix_environment.inproc_session(store_uri=uri) as nix,
            nix.store() as store,
            nix.eval(store) as evaluator,
        ):
            assert await evaluator.string("builtins.test_release_probe 1") is not None

    # The first session opens what stays open for the process, such as the
    # collector's own files.
    await one_session()
    before = await _open_descriptors()
    for _ in range(5):
        await one_session()
    assert await _open_descriptors() == before
