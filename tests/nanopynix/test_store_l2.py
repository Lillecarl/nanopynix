"""Integration tests for the L2 Store facade via Session."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false
# nanopynix / nanopynix_proto are C++ nanobind extensions without type stubs.
# Variable types and isinstance checks involving C++ types are inherently unresolvable.

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    GetStoreDirRequest,
    GetUriRequest,
    IsValidPathRequest,
    OptimiseStoreRequest,
    ParseStorePathRequest,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryPathInfoRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    VerifyStoreRequest,
)

from nanopynix import MissingInfo, PathInfo, Session, StorePath, build_info


NIX_GC_ROOTS_BUG = pytest.mark.skipif(
    build_info()["nix_version"].startswith(("2.31.", "2.34.")),
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
            response = await store.get_uri(GetUriRequest())
            assert isinstance(response.uri, str)
            assert len(response.uri) > 0


async def test_get_store_dir():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            response = await store.get_store_dir(GetStoreDirRequest())
            assert response.dir == "/nix/store"


async def test_parse_store_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                response = await store.parse_store_path(ParseStorePathRequest(path=str(path)))
                assert response.path == str(path)


async def test_is_valid_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            valid_paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if valid_paths:
                path = StorePath(valid_paths[0])
                response = await store.is_valid_path(IsValidPathRequest(path=str(path)))
                assert response.valid


async def test_query_path_info():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                pi = await store.query_path_info(QueryPathInfoRequest(path=str(path)))
                assert isinstance(pi, PathInfo)
                assert isinstance(pi.path, str)
                assert pi.nar_size >= 0


async def test_query_path_from_hash_part():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                hp = StorePath(paths[0]).hash_part
                response = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hp))
                assert isinstance(response.path, str)


async def test_compute_fs_closure():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                closure = (await store.compute_fs_closure(ComputeFsClosureRequest(path=str(path)))).paths
                assert isinstance(closure, list)
                assert len(closure) >= 1  # type: ignore[reportUnknownArgumentType] -- generated protobuf paths field is list[Unknown]
                assert all(isinstance(sp, str) for sp in closure)


async def test_query_missing():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            mi = await store.query_missing(
                QueryMissingRequest(derived_paths=["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
            )
            assert isinstance(mi, MissingInfo)


async def test_query_missing_accepts_serialized_derived_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            derivation = next((path for path in paths if StorePath(path).is_derivation), None)
            if derivation is not None:
                result = await store.query_missing(QueryMissingRequest(derived_paths=[f"{derivation}^out"]))
                assert isinstance(result, MissingInfo)


async def test_query_derived_outputs():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            drvs = [path for path in (StorePath(path) for path in paths) if path.is_derivation]
            if drvs:
                outputs = (
                    await store.query_derivation_outputs(QueryDerivationOutputsRequest(path=str(drvs[0])))
                ).paths
                assert isinstance(outputs, list)


async def test_query_valid_derivers():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                derivers = (await store.query_valid_derivers(QueryValidDeriversRequest(path=str(path)))).paths
                assert isinstance(derivers, list)


async def test_query_referrers():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                refs = (await store.query_referrers(QueryReferrersRequest(path=str(path)))).paths
                assert isinstance(refs, list)


async def test_query_substitutable_paths():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                subs = (await store.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=[str(path)]))).paths
                assert isinstance(subs, list)


async def test_follow_links_to_store_path(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                target = paths[0]
                link = tmp_path / "store-path"
                link.symlink_to(target)

                response = await store.follow_links_to_store_path(FollowLinksToStorePathRequest(path=str(link)))
                assert response.path == target


async def test_store_path_str_and_model_roundtrip():
    """StorePath accepts a str or a StorePath model as argument."""
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                sp = StorePath(paths[0])
                assert StorePath(sp) is sp
                assert (await store.is_valid_path(IsValidPathRequest(path=str(sp)))).valid is True


async def test_add_temp_root():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                await store.add_temp_root(AddTempRootRequest(path=str(path)))


@NIX_GC_ROOTS_BUG
async def test_find_roots(tmp_path: Path):
    async with Session(store_uri=f"local?root={tmp_path}") as session:
        store: Any
        async with session.store() as store:
            roots = (await store.find_roots(FindRootsRequest(censor=True))).roots
            assert isinstance(roots, list)
            for root in roots[:10]:
                assert isinstance(root.link, str)
                assert isinstance(root.path, str)


@NIX_GC_ROOTS_BUG
async def test_collect_garbage_return_dead_does_not_delete(tmp_path: Path):
    async with Session(store_uri=f"local?root={tmp_path}") as session:
        store: Any
        async with session.store() as store:
            result = await store.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_DEAD))
            assert isinstance(result.paths, list)
            assert result.bytes_freed == 0


@pytest.mark.live_gc
async def test_collect_garbage_delete_dead_live_store_requires_opt_in():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            result = await store.collect_garbage(CollectGarbageRequest(action=GcAction.DELETE_DEAD, max_freed=1))
            assert isinstance(result.paths, list)


async def test_add_perm_root_and_indirect_root(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                root_path = tmp_path / "nanopynix-gc-root"
                response = await store.add_perm_root(AddPermRootRequest(store_path=str(path), gc_root=str(root_path)))
                assert response.path == str(root_path)
                assert root_path.is_symlink()
                await store.add_indirect_root(AddIndirectRootRequest(path=str(root_path)))


async def test_ensure_path():
    async with Session() as session:
        store: Any
        async with session.store() as store:
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])
                await store.ensure_path(EnsurePathRequest(path=str(path)))


async def test_optimise_store_on_empty_local_store(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store(f"local?root={tmp_path}") as store:
            await store.optimise_store(OptimiseStoreRequest())


async def test_verify_store_on_empty_local_store(tmp_path: Path):
    async with Session() as session:
        store: Any
        async with session.store(f"local?root={tmp_path}") as store:
            response = await store.verify_store(VerifyStoreRequest(check_contents=False, repair=False))
            assert response.errors is False
