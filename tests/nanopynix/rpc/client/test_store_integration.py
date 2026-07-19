"""Integration tests for the L2 Store facade via Session."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false
# nanopynix / nanopynix_proto are C++ nanobind extensions without type stubs.
# Variable types and isinstance checks involving C++ types are inherently unresolvable.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_proto.nix.store import (
    AddIndirectRootRequest,
    AddPermRootRequest,
    AddTempRootRequest,
    CollectGarbageRequest,
    ComputeFsClosureRequest,
    EnsurePathRequest,
    FindRootsRequest,
    FollowLinksToStorePathRequest,
    GcAction,
    OptimiseStoreRequest,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    VerifyStoreRequest,
)

from nanopynix import Derivation, GcResult, MissingInfo, PathInfo, Session, StorePath, build_info

if TYPE_CHECKING:
    from pathlib import Path

NIX_GC_ROOTS_BUG = pytest.mark.skipif(
    build_info()["nix_version"].startswith(("2.31.", "2.34.")),  # type: ignore[reportUnknownArgumentType] -- build_info from C++ extension
    reason="Nix 2.31 and 2.34 findRoots/collectGarbage crash on nonnumeric temproots filenames; https://github.com/NixOS/nix/issues/16138",
)


async def test_open_close():
    session = Session()
    await session.open()
    store: Any
    async with session.store() as store:
        assert store is not None
    await session.close()


async def test_context_manager():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            assert store is not None


async def test_get_uri():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            uri = await store.uri()
            assert isinstance(uri, str)
            assert len(uri) > 0


async def test_get_store_dir():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            store_dir = await store.store_dir()
            assert store_dir == "/nix/store"


async def test_parse_store_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                sp = await store.parse_store_path(str(path))
                assert str(sp) == str(path)


async def test_is_valid_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            valid_paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if valid_paths:
                path = StorePath(valid_paths[0])
                valid = await store.is_valid_path(str(path))
                assert valid


async def test_query_path_info():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                pi = await store.query_path_info(str(path))
                assert isinstance(pi, PathInfo)
                assert isinstance(pi.path, str)
                assert pi.nar_size >= 0


async def test_query_path_from_hash_part():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                hp = StorePath(paths[0]).hash_part
                response = await store.rpc.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hp))
                assert isinstance(response.path, str)


async def test_compute_fs_closure():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                closure = (await store.rpc.compute_fs_closure(ComputeFsClosureRequest(path=str(path)))).paths
                assert isinstance(closure, list)
                assert len(closure) >= 1  # type: ignore[reportUnknownArgumentType] -- generated protobuf paths field is list[Unknown]
                assert all(isinstance(sp, str) for sp in closure)


async def test_query_missing():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            mi = await store.rpc.query_missing(
                QueryMissingRequest(derived_paths=["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
            )
            assert isinstance(mi, MissingInfo)


async def test_query_missing_accepts_serialized_derived_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            derivation = next((path for path in paths if StorePath(path).is_derivation), None)
            if derivation is not None:
                result = await store.rpc.query_missing(QueryMissingRequest(derived_paths=[f"{derivation}^out"]))
                assert isinstance(result, MissingInfo)


async def test_query_derived_outputs():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            drvs = [path for path in (StorePath(path) for path in paths) if path.is_derivation]
            if drvs:
                outputs = (
                    await store.rpc.query_derivation_outputs(QueryDerivationOutputsRequest(path=str(drvs[0])))
                ).paths
                assert isinstance(outputs, list)


async def test_query_valid_derivers():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                derivers = (await store.rpc.query_valid_derivers(QueryValidDeriversRequest(path=str(path)))).paths
                assert isinstance(derivers, list)


async def test_query_referrers():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                refs = (await store.rpc.query_referrers(QueryReferrersRequest(path=str(path)))).paths
                assert isinstance(refs, list)


async def test_query_substitutable_paths():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                subs = (await store.rpc.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=[str(path)]))).paths
                assert isinstance(subs, list)


async def test_follow_links_to_store_path(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                target = paths[0]
                link = tmp_path / "store-path"
                link.symlink_to(target)

                response = await store.rpc.follow_links_to_store_path(FollowLinksToStorePathRequest(path=str(link)))
                assert response.path == target


async def test_store_path_str_and_model_roundtrip():
    """StorePath accepts a str or a StorePath model as argument."""
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                sp = StorePath(paths[0])
                assert StorePath(sp) is sp
                assert await store.is_valid_path(str(sp)) is True


async def test_add_temp_root():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                await store.rpc.add_temp_root(AddTempRootRequest(path=str(path)))


@NIX_GC_ROOTS_BUG
async def test_find_roots(tmp_path: Path):
    async with Session(store_uri=f"local?root={tmp_path}") as session:
        store: Any
        async with session.store() as store:
            roots = (await store.rpc.find_roots(FindRootsRequest(censor=True))).roots
            assert isinstance(roots, list)
            for root in roots[:10]:
                assert isinstance(root.link, str)
                assert isinstance(root.path, str)


@NIX_GC_ROOTS_BUG
async def test_collect_garbage_return_dead_does_not_delete(tmp_path: Path):
    async with Session(store_uri=f"local?root={tmp_path}") as session:
        store: Any
        async with session.store() as store:
            result = await store.rpc.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_DEAD))
            assert isinstance(result.paths, list)
            assert result.bytes_freed == 0


async def test_public_query_missing():
    """Public store.query_missing returns a MissingInfo model."""
    async with Session() as session:
        store: Any
        async with session.store() as store:
            mi = await store.query_missing(["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
            assert isinstance(mi, MissingInfo)
            assert isinstance(mi.will_build, list)
            assert isinstance(mi.will_substitute, list)
            assert isinstance(mi.unknown, list)


async def test_public_query_missing_accepts_storepath():
    """query_missing accepts StorePath instances."""
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                sp = StorePath(paths[0])
                mis = await store.query_missing([sp])
                assert isinstance(mis, MissingInfo)


async def test_public_read_derivation():
    """Public store.read_derivation returns a Derivation model."""
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            drv = next((p for p in paths if StorePath(p).is_derivation), None)
            if drv is not None:
                d = await store.read_derivation(drv)
                assert isinstance(d, Derivation)
                assert d.name
                assert d.system


@NIX_GC_ROOTS_BUG
async def test_public_collect_garbage_return_dead(tmp_path: Path):
    """Public store.collect_garbage(GcAction.RETURN_DEAD) returns GcResult."""
    async with Session(store_uri=f"local?root={tmp_path}") as session:
        store: Any
        async with session.store() as store:
            result = await store.collect_garbage(GcAction.RETURN_DEAD)
            assert isinstance(result, GcResult)
            assert isinstance(result.paths, list)
            assert result.bytes_freed == 0


@pytest.mark.live_gc
async def test_collect_garbage_delete_dead_live_store_requires_opt_in():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            result = await store.rpc.collect_garbage(CollectGarbageRequest(action=GcAction.DELETE_DEAD, max_freed=1))
            assert isinstance(result.paths, list)


async def test_add_perm_root_and_indirect_root(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                root_path = tmp_path / "nanopynix-gc-root"
                response = await store.rpc.add_perm_root(AddPermRootRequest(store_path=str(path), gc_root=str(root_path)))
                assert response.path == str(root_path)
                assert root_path.is_symlink()
                await store.rpc.add_indirect_root(AddIndirectRootRequest(path=str(root_path)))


async def test_ensure_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.rpc.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                await store.rpc.ensure_path(EnsurePathRequest(path=str(path)))


async def test_optimise_store_on_empty_local_store(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store(f"local?root={tmp_path}") as store:
            await store.rpc.optimise_store(OptimiseStoreRequest())


async def test_verify_store_on_empty_local_store(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store(f"local?root={tmp_path}") as store:
            response = await store.rpc.verify_store(VerifyStoreRequest(check_contents=False, repair=False))
            assert response.errors is False
