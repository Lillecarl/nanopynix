"""An empty store path must raise, never abort the process.

``nix::StoreDirConfig::parseStorePath()`` canonicalises before it validates,
and ``canonPath()`` asserts its argument is non-empty. Nix wraps
``__assert_fail``, so violating that assertion is ``abort()`` -- SIGABRT, not a
C++ exception. Nothing downstream can catch it: an in-process caller loses the
whole interpreter, not just the call.

Before the guard in ``store_path_from_string``, 16 of the store methods that
accept a path died that way on ``""``. Every *other* malformed path already
raised, so the empty string -- reachable from an unset variable, a blank config
field, or a stripped-to-nothing line of input -- was the single input that
crashed instead of erroring.

These tests run each call in the parent process on purpose. A regression does
not fail them; it takes pytest down with it, which is louder and impossible to
mistake for an ordinary failure.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

import pytest
from nanopynix_bindings.errors import BadStorePath

from nanopynix import inproc
from nanopynix.exceptions import BadStorePathError, NixError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from tests.support.nix_environment import InprocSessionFactory

# Every store entry point that turns a caller-supplied string into a
# nix::StorePath. Named rather than discovered, so that adding a path-taking
# method is a deliberate addition here --
# ``test_the_call_table_covers_every_path_taking_method`` fails until it is.
EMPTY_PATH_CALLS: dict[str, Callable[[Any, Path], Awaitable[object]]] = {
    "parse_store_path": lambda store, _tmp: store.parse_store_path(""),
    "is_valid_path": lambda store, _tmp: store.is_valid_path(""),
    "query_path_info": lambda store, _tmp: store.query_path_info(""),
    "compute_fs_closure": lambda store, _tmp: store.compute_fs_closure(""),
    "query_referrers": lambda store, _tmp: store.query_referrers(""),
    "query_valid_derivers": lambda store, _tmp: store.query_valid_derivers(""),
    "query_derivation_outputs": lambda store, _tmp: store.query_derivation_outputs(""),
    "query_substitutable_paths": lambda store, _tmp: store.query_substitutable_paths([""]),
    "query_missing": lambda store, _tmp: store.query_missing([""]),
    "read_derivation": lambda store, _tmp: store.read_derivation(""),
    "get_build_log": lambda store, _tmp: store.get_build_log(""),
    "add_temp_root": lambda store, _tmp: store.add_temp_root(""),
    "ensure_path": lambda store, _tmp: store.ensure_path(""),
    "follow_links_to_store_path": lambda store, _tmp: store.follow_links_to_store_path(""),
    "add_to_store": lambda store, _tmp: store.add_to_store(""),
    "compute_store_path": lambda store, _tmp: store.compute_store_path(""),
    "add_perm_root": lambda store, tmp: store.add_perm_root("", str(tmp / "gcroot")),
    "build_paths_with_results": lambda store, _tmp: store.build_paths_with_results([""]),
    "copy_closure": lambda store, _tmp: store.copy_closure([""], store),
}

# Path-taking by signature, but not store-path-taking, so the guard does not
# apply and BadStorePathError is not the right answer. Listed explicitly so the
# coverage check below can tell "deliberately excluded" from "forgotten".
NOT_STORE_PATHS: dict[str, str] = {
    "add_indirect_root": "takes the filesystem path of a GC root symlink, not a store path.",
    "query_path_from_hash_part": "takes a bare hash component, which never reaches parseStorePath.",
}


@pytest.mark.parametrize("method", sorted(EMPTY_PATH_CALLS))
async def test_empty_path_raises_instead_of_aborting(
    method: str,
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """``""`` must arrive as BadStorePathError, the same as any other bad path."""
    async with inproc_session() as session, session.store() as store:
        with pytest.raises(BadStorePathError):
            await EMPTY_PATH_CALLS[method](store, tmp_path)


# The three inputs that already raised before the guard, with the exact type
# each produced. Pinning the type is the point: if the empty-string check
# altered any of them, the fix traded one inconsistency for another.
UNCHANGED_MALFORMED_PATHS: list[tuple[str, type[Exception]]] = [
    ("x", NixError),
    ("/etc/passwd", BadStorePathError),
    ("garbage", NixError),
]


@pytest.mark.parametrize(("path", "expected"), UNCHANGED_MALFORMED_PATHS)
async def test_other_malformed_paths_keep_their_original_error_type(
    path: str,
    expected: type[Exception],
    inproc_session: InprocSessionFactory,
) -> None:
    """The guard must not have changed what the already-working inputs do."""
    async with inproc_session() as session, session.store() as store:
        with pytest.raises(NixError) as caught:
            await store.parse_store_path(path)
        assert type(caught.value) is expected, f"{path!r} now raises {type(caught.value).__name__}"


async def test_the_dict_api_rejects_an_empty_path_too(
    inproc_session: InprocSessionFactory,
) -> None:
    """The proto-shaped entry point shares the funnel, so it shares the guard.

    ``inproc`` reaches C++ through the direct signatures and the rpc worker
    through the dict ones. Both call ``parseStorePath``, so a guard on only one
    of them would leave the crash reachable from the other engine.

    Going through the raw object deliberately skips ``_run_with_log_context``,
    inproc's translation chokepoint, so what surfaces here is the untranslated
    binding error -- which is the point: the guard is in C++, not in the Python
    wrapper that usually renames its exceptions.
    """
    async with inproc_session() as session, session.store() as store:
        raw = cast("Any", store._require_raw())
        with pytest.raises(BadStorePath, match="must not be empty"):
            raw.store_is_valid_path({"path": ""})


def test_the_call_table_covers_every_path_taking_method() -> None:
    """A path-taking method with no entry above is a method nobody proved safe.

    The crash was systemic precisely because every path-taking method shared
    one funnel, so partial coverage here would be a false negative waiting to
    happen. Discovery is by signature; the table and the documented exclusions
    together have to account for all of it.
    """
    path_params = {"path", "paths", "drv_path", "derived_paths", "hash_part"}
    discovered: set[str] = set()
    for name, member in inspect.getmembers(inproc.Store, callable):
        if name.startswith("_"):
            continue
        try:
            parameters = set(inspect.signature(member).parameters)
        except (TypeError, ValueError):  # C-level callables have no signature
            continue
        if parameters & path_params:
            discovered.add(name)

    assert discovered, "signature discovery found nothing -- the check is vacuous"
    accounted = set(EMPTY_PATH_CALLS) | set(NOT_STORE_PATHS)
    assert discovered <= accounted, (
        f"path-taking store methods with no empty-string coverage: {sorted(discovered - accounted)}"
    )
    assert set(EMPTY_PATH_CALLS) <= discovered, (
        f"table lists methods that no longer exist: {sorted(set(EMPTY_PATH_CALLS) - discovered)}"
    )
