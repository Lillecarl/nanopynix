"""Does the lock graph reach Python, and does it say what Nix says?

``LockedFlake`` used to offer an ``inputs`` map filled from
``locked->flake.inputs``, which is what a ``flake.nix`` *declares*. The name
said locked and the content was the original reference: no ``rev``, no
``narHash``, and -- being a flat map of the top level -- nowhere to put a
transitive node or a ``follows`` edge.

Two doors replace it, and this file asks a different question of each:

* :meth:`~nanopynix.protocols.AsyncLockedFlake.metadata_json` renders the whole
  graph, because Nix renders it. The oracle for that is
  ``nix flake metadata --json``, and it lives in ``pynix/tests/test_flake_metadata.py``
  where the command that prints it lives.
* :meth:`~nanopynix.protocols.AsyncLockedFlake.find_input` answers one question
  about the graph. That is the door ``pynix develop`` needs, and it is what
  this file covers.

The fixture is three local git flakes (:func:`test_support.git_fixtures.init_linked_flakes`),
so nothing here reaches the network.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from test_support.git_fixtures import init_linked_flakes

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import InprocSessionFactory, RpcSessionFactory

# Every test here locks a flake, which needs an evaluator.
pytestmark = pytest.mark.evaluator_in_process


async def test_find_input_returns_a_locked_ref_with_a_rev(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """A direct input must come back locked, and not as the caller declared it.

    The declared reference is ``git+file:///...`` with no revision. The locked
    one pins a commit, which is the whole difference between the map this
    replaced and the lock file.
    """
    flakes = init_linked_flakes(tmp_path)
    async with inproc_session() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(str(flakes.root), write_lock_file=False)
        try:
            node = await locked.find_input(["leaf"])
        finally:
            await locked.release()

    assert node is not None
    assert node.is_flake
    assert "rev=" in node.locked_ref, node.locked_ref
    assert "rev=" not in node.original_ref, node.original_ref
    assert str(flakes.leaf) in node.locked_ref


async def test_find_input_returns_none_for_a_name_that_is_not_an_input(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """``pynix develop`` relies on this: a flake need not declare ``nixpkgs``."""
    flakes = init_linked_flakes(tmp_path)
    async with inproc_session() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(str(flakes.root), write_lock_file=False)
        try:
            assert await locked.find_input(["nixpkgs"]) is None
        finally:
            await locked.release()


async def test_find_input_resolves_a_follows_edge(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """``mid.leaf`` follows the root's ``leaf``, so both paths reach one node.

    This is the shape the flat map could not express at all, and it is why
    ``find_input`` is bound rather than walked in Python: Nix's ``doFind``
    resolves a ``follows`` edge by recursing from the root, and a walk over the
    rendered graph would have to derive that again.
    """
    flakes = init_linked_flakes(tmp_path)
    async with inproc_session() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(str(flakes.root), write_lock_file=False)
        try:
            direct = await locked.find_input(["leaf"])
            through_mid = await locked.find_input(["mid", "leaf"])
            mid = await locked.find_input(["mid"])
        finally:
            await locked.release()

    assert direct is not None
    assert through_mid is not None
    assert through_mid.locked_ref == direct.locked_ref
    # And the edge really is an edge: `mid` itself is a different node.
    assert mid is not None
    assert mid.locked_ref != direct.locked_ref


async def test_both_engines_answer_the_same(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
    tmp_path: Path,
) -> None:
    """The rpc engine must not lose the graph on the way through the worker."""
    flakes = init_linked_flakes(tmp_path)

    async with inproc_session() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(str(flakes.root), write_lock_file=False)
        try:
            from_inproc = await locked.find_input(["mid", "leaf"])
            inproc_metadata = json.loads(await locked.metadata_json())
        finally:
            await locked.release()

    async with rpc_session() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(str(flakes.root), write_lock_file=False)
        try:
            from_rpc = await locked.find_input(["mid", "leaf"])
            rpc_metadata = json.loads(await locked.metadata_json())
        finally:
            await locked.release()

    assert from_inproc is not None
    assert from_rpc == from_inproc
    assert rpc_metadata == inproc_metadata


async def test_metadata_json_carries_the_whole_graph(
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """``locks`` must hold every node, and the ``follows`` edge as an edge.

    ``nix flake metadata --json`` is the oracle for the exact bytes, in
    ``pynix/tests/test_flake_metadata.py``. What this pins is the property that made the
    old map wrong: the graph reaches Python whole.

    ``LockFile::toJSON`` writes a ``follows`` edge as a *list* -- the attribute
    path it points at -- and a node edge as the *string* key of that node. That
    is what makes the two distinguishable, and it is what the old ``follows``
    list of declared names could not say.
    """
    flakes = init_linked_flakes(tmp_path)
    async with inproc_session() as session, session.store() as store, session.eval(store) as evaluator:
        locked = await evaluator.lock_flake(str(flakes.root), write_lock_file=False)
        try:
            metadata = json.loads(await locked.metadata_json())
        finally:
            await locked.release()

    assert metadata["description"] == "the root of a linked flake graph"

    locks = metadata["locks"]
    nodes = locks["nodes"]
    root = nodes[locks["root"]]
    assert set(root["inputs"]) == {"leaf", "mid"}

    # The root's own edges name nodes, so each is a string.
    leaf_key = root["inputs"]["leaf"]
    mid_key = root["inputs"]["mid"]
    assert isinstance(leaf_key, str)
    assert isinstance(mid_key, str)

    # `mid`'s `leaf` follows the root's, so that edge is a path, not a node.
    assert nodes[mid_key]["inputs"]["leaf"] == ["leaf"]

    # And the node behind the root's `leaf` carries what the declared map never
    # did: a revision and a NAR hash.
    assert "rev" in nodes[leaf_key]["locked"]
    assert "narHash" in nodes[leaf_key]["locked"]
    assert "rev" not in nodes[leaf_key]["original"]
