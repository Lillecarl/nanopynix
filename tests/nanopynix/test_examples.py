"""Run doc examples as integration tests to prevent staleness."""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest
from nanopynix_bindings.store import open_store

import nanopynix

_EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"
if not _EXAMPLES.is_dir():
    pytest.skip("examples directory not found", allow_module_level=True)

_EXAMPLE_FILES = sorted(_EXAMPLES.glob("*_example.py"))

# `primops_example.py` needs dynamic primop registration, including the
# built-in YAML primops — see nanopynix.primops.
_requires_dynamic_primops = pytest.mark.nix_capability("dynamic_primop_registration")
_EXAMPLE_PARAMS = [
    pytest.param(path, marks=_requires_dynamic_primops) if path.name == "primops_example.py" else path
    for path in _EXAMPLE_FILES
]


def _seed_isolated_store(root: Path) -> str:
    """Build a from-scratch, single-path, GC-rooted store under *root*.

    ``store_example.py`` asserts on real store content (at least one valid
    path, a nonempty ``collect_garbage(RETURN_LIVE)`` result), so an empty
    isolated store won't do -- this gives it exactly one seeded, rooted path
    to work with. Runs via the sync L1 binding directly in this (already
    forked, see ``test_example_runs``) test process; the example itself
    still talks to its own separate worker subprocess via the async
    ``Session``/RPC API, so there is no shared libstore init state between
    the two.
    """
    nanopynix.init_libstore()
    store_uri = f"local://?root={root}"
    store = open_store(store_uri)
    try:
        seed_file = root.parent / "example-seed.txt"
        seed_file.write_text("nanopynix doc-example fixture\n", encoding="utf-8")
        added = store.add_to_store(str(seed_file), name="example-seed", method="flat", hash_algo="sha256")
        gc_root = root.parent / "example-gcroot"
        store.add_perm_root(added, str(gc_root))
    finally:
        store.close()
    return store_uri


@pytest.mark.forked
@pytest.mark.parametrize("path", _EXAMPLE_PARAMS, ids=lambda p: p.name)
def test_example_runs(path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if path.name == "store_example.py":
        # Isolates this example from the real system store -- without this,
        # Session()'s "auto" default hits the ambient host Nix daemon, and
        # query_all_valid_paths()/collect_garbage(RETURN_LIVE) scale with
        # *that* store's real size (tens of thousands of paths on a
        # long-lived dev machine), not this test's needs.
        store_uri = _seed_isolated_store(tmp_path / "store")
        real_session_cls = nanopynix.rpc.Session

        def _isolated_session(*args: Any, **kwargs: Any) -> nanopynix.rpc.Session:
            kwargs.setdefault("store_uri", store_uri)
            kwargs.setdefault("load_config", False)
            return real_session_cls(*args, **kwargs)

        monkeypatch.setattr(nanopynix.rpc, "Session", _isolated_session)

    # The examples are synchronous entry points that call asyncio.run() themselves, executed here by runpy. Draining
    # whatever tasks they leave pending needs a loop object this test still holds a reference to, installed as the
    # process default so the example's own asyncio.run() picks it up -- anyio has no installable-default-loop concept,
    # because owning the loop is precisely what anyio.run() does instead.
    loop = asyncio.new_event_loop()  # noqa: TID251 -- see above; the example script, not this test, runs the loop
    sys.path.insert(0, str(_EXAMPLES))
    try:
        asyncio.set_event_loop(loop)  # noqa: TID251 -- see above; runpy needs this installed as the process default
        runpy.run_path(str(path), run_name="__main__")
    finally:
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        sys.path.remove(str(_EXAMPLES))
