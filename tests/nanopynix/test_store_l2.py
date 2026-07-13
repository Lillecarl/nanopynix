"""Integration tests for the L2 Store facade via Session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nanopynix_proto.nix.common import StorePath as StorePathProto
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

from nanopynix import MissingInfo, PathInfo, Session, StorePath  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs


async def test_open_close():
    session = Session()  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
    await session.open()
    store: Any
    async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        assert store is not None
    await session.close()


async def test_context_manager():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            assert store is not None


async def test_get_uri():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            response = await store.get_uri(GetUriRequest())
            assert isinstance(response.uri, str)
            assert len(response.uri) > 0


async def test_get_store_dir():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            response = await store.get_store_dir(GetStoreDirRequest())
            assert response.dir == "/nix/store"


async def test_parse_store_path():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                sp = await store.parse_store_path(ParseStorePathRequest(path=path.to_string))
                assert isinstance(sp, StorePathProto)
                assert path.to_string == StorePath(sp).to_string  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs


async def test_is_valid_path():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            valid_paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if valid_paths:
                path = StorePath(valid_paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                response = await store.is_valid_path(IsValidPathRequest(path=path.to_string))
                assert response.valid


async def test_query_path_info():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                pi = await store.query_path_info(QueryPathInfoRequest(path=path.to_string))
                assert isinstance(pi, PathInfo)  # type: ignore[reportUnknownMemberType]  # PathInfo from nanopynix nanobind
                assert isinstance(pi.path, StorePathProto)
                assert pi.nar_size >= 0


async def test_query_path_from_hash_part():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                hp = StorePath(paths[0]).hash_part  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                response = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hp))
                sp = response.path
                assert isinstance(sp, StorePathProto)


async def test_compute_fs_closure():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                closure = (await store.compute_fs_closure(ComputeFsClosureRequest(path=path.to_string))).paths
                assert isinstance(closure, list)
                assert len(closure) >= 1
                assert all(isinstance(sp, StorePathProto) for sp in closure)


async def test_query_missing():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            mi = await store.query_missing(
                QueryMissingRequest(paths=["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
            )
            assert isinstance(mi, MissingInfo)  # type: ignore[reportUnknownMemberType]  # MissingInfo from nanopynix nanobind


async def test_query_derived_outputs():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            drvs = [path for path in (StorePath(path) for path in paths) if path.is_derivation]  # type: ignore[reportUnknownMemberType]  # StorePath from nanobind
            if drvs:
                outputs = (
                    await store.query_derivation_outputs(QueryDerivationOutputsRequest(path=drvs[0].to_string))
                ).paths
                assert isinstance(outputs, list)


async def test_query_valid_derivers():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                derivers = (await store.query_valid_derivers(QueryValidDeriversRequest(path=path.to_string))).paths
                assert isinstance(derivers, list)


async def test_query_referrers():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                refs = (await store.query_referrers(QueryReferrersRequest(path=path.to_string))).paths
                assert isinstance(refs, list)


async def test_query_substitutable_paths():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                subs = (await store.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=[path.to_string]))).paths
                assert isinstance(subs, list)


async def test_follow_links_to_store_path():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                sp = await store.follow_links_to_store_path(FollowLinksToStorePathRequest(path="/run/current-system"))
                assert isinstance(sp, StorePathProto)


async def test_store_path_str_and_model_roundtrip():
    """StorePath accepts a str or a StorePath model as argument."""
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                sp = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                assert StorePath(sp) is sp  # type: ignore[reportUnknownMemberType]  # StorePath from nanobind
                assert (await store.is_valid_path(IsValidPathRequest(path=sp.to_string))).valid is True


async def test_add_temp_root():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                await store.add_temp_root(AddTempRootRequest(path=path.to_string))


async def test_find_roots():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            roots = (await store.find_roots(FindRootsRequest(censor=True))).roots
            assert isinstance(roots, list)
            for root in roots[:10]:
                assert isinstance(root.link, str)
                assert isinstance(root.path, StorePathProto)


async def test_collect_garbage_return_dead_does_not_delete():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            result = await store.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_DEAD))
            assert isinstance(result.paths, list)
            assert result.bytes_freed == 0


@pytest.mark.live_gc
async def test_collect_garbage_delete_dead_live_store_requires_opt_in():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            result = await store.collect_garbage(CollectGarbageRequest(action=GcAction.DELETE_DEAD, max_freed=1))
            assert isinstance(result.paths, list)


async def test_add_perm_root_and_indirect_root(tmp_path: Path):
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                root_path = tmp_path / "nanopynix-gc-root"
                response = await store.add_perm_root(AddPermRootRequest(store_path=path.to_string, gc_root=str(root_path)))
                assert response.path == str(root_path)
                assert root_path.is_symlink()
                await store.add_indirect_root(AddIndirectRootRequest(path=str(root_path)))


async def test_ensure_path():
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store() as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
            if paths:
                path = StorePath(paths[0])  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
                await store.ensure_path(EnsurePathRequest(path=path.to_string))


async def test_optimise_store_on_empty_local_store(tmp_path: Path):
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store(f"local?root={tmp_path}") as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            await store.optimise_store(OptimiseStoreRequest())


async def test_verify_store_on_empty_local_store(tmp_path: Path):
    async with Session() as session:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
        store: Any
        async with session.store(f"local?root={tmp_path}") as store:  # type: ignore[reportUnknownMemberType]  # nanopynix nanobind extension without stubs
            response = await store.verify_store(VerifyStoreRequest(check_contents=False, repair=False))
            assert response.errors is False
