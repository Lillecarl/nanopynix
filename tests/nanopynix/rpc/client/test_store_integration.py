"""Integration tests for the L2 Store facade against hermetic stores."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnnecessaryIsInstance=false
# nanopynix / nanopynix_proto are C++ nanobind extensions without type stubs.

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
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
    VerifyStoreRequest,
)

from nanopynix import Derivation, GcResult, MissingInfo, PathInfo, StorePath

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.nix_environment import NixTestEnvironment

NIX_GC_ROOTS_BUG = pytest.mark.nix_version(
    exclude=("2.31", "2.34"),
    reason="findRoots/collectGarbage crash on nonnumeric temproots filenames; https://github.com/NixOS/nix/issues/16138",
)


async def _create_derivation(eval: Any) -> StorePath:
    path_value = await eval.string(
        """
          builtins.unsafeDiscardStringContext (
            (derivation {
              name = "nanopynix-test-derivation";
              system = builtins.currentSystem;
              builder = "/not-used-by-this-test";
            }).drvPath
          )
        """
    )
    result = await path_value.force()
    if not isinstance(result, str):
        raise TypeError(f"test derivation did not evaluate to a store path: {result!r}")
    path = StorePath(result)
    if not path.is_derivation:
        raise RuntimeError(f"test derivation is not a .drv path: {path}")
    return path


async def test_open_close(shared_nix_environment: NixTestEnvironment) -> None:
    session = shared_nix_environment.rpc_session()
    await session.open()
    store: Any
    async with session.store() as store:
        assert store is not None
    await session.close()


async def test_context_manager(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session:
        store: Any
        async with session.store() as store:
            assert store is not None


async def test_get_uri_and_store_dir(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        uri = await store.uri()
        assert isinstance(uri, str)
        assert uri
        assert await store.store_dir() == "/nix/store"


async def test_parse_store_path_and_is_valid_path(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        parsed = await store.parse_store_path(str(seeded_store_path))
        assert str(parsed) == str(seeded_store_path)
        assert await store.is_valid_path(str(seeded_store_path))


async def test_query_path_info_and_hash_part(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        info = await store.query_path_info(str(seeded_store_path))
        assert isinstance(info, PathInfo)
        assert info.path == str(seeded_store_path)
        assert info.nar_size > 0
        response = await store.rpc.query_path_from_hash_part(
            QueryPathFromHashPartRequest(hash_part=seeded_store_path.hash_part)
        )
        assert response.path == str(seeded_store_path)


async def test_compute_fs_closure(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        closure = (await store.rpc.compute_fs_closure(ComputeFsClosureRequest(path=str(seeded_store_path)))).paths
        assert str(seeded_store_path) in closure


async def test_query_missing(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        missing = await store.rpc.query_missing(
            QueryMissingRequest(derived_paths=["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
        )
        assert isinstance(missing, MissingInfo)


async def test_query_missing_accepts_serialized_derived_path(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        derivation_path = await _create_derivation(eval)
        missing = await store.rpc.query_missing(QueryMissingRequest(derived_paths=[f"{derivation_path}^out"]))
        assert isinstance(missing, MissingInfo)


async def test_derivation_queries(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with (
        shared_nix_environment.rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        derivation_path = await _create_derivation(eval)
        outputs = (
            await store.rpc.query_derivation_outputs(QueryDerivationOutputsRequest(path=str(derivation_path)))
        ).paths
        assert len(outputs) == 1
        derivers = (await store.rpc.query_valid_derivers(QueryValidDeriversRequest(path=outputs[0]))).paths
        assert isinstance(derivers, list)
        derivation = await store.read_derivation(derivation_path)
        assert isinstance(derivation, Derivation)
        assert derivation.name == "nanopynix-test-derivation"


async def test_query_referrers_and_substitutable_paths(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        referrers = (await store.rpc.query_referrers(QueryReferrersRequest(path=str(seeded_store_path)))).paths
        assert isinstance(referrers, list)
        substitutable = (
            await store.rpc.query_substitutable_paths(QuerySubstitutablePathsRequest(paths=[str(seeded_store_path)]))
        ).paths
        assert isinstance(substitutable, list)


async def test_follow_links_to_store_path(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
    tmp_path: Path,
) -> None:
    link = tmp_path / "store-path"
    link.symlink_to(seeded_store_path)
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        response = await store.rpc.follow_links_to_store_path(FollowLinksToStorePathRequest(path=str(link)))
        assert response.path == str(seeded_store_path)


async def test_store_path_model_roundtrip(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        assert StorePath(seeded_store_path) is seeded_store_path
        assert await store.is_valid_path(seeded_store_path)


async def test_add_temp_root(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        await store.rpc.add_temp_root(AddTempRootRequest(path=str(seeded_store_path)))


@NIX_GC_ROOTS_BUG
async def test_find_roots(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        roots = (await store.rpc.find_roots(FindRootsRequest(censor=True))).roots
        assert isinstance(roots, list)
        for root in roots:
            assert isinstance(root.link, str)
            assert isinstance(root.path, str)


@NIX_GC_ROOTS_BUG
async def test_collect_garbage_return_dead(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        result = await store.rpc.collect_garbage(CollectGarbageRequest(action=GcAction.RETURN_DEAD))
        assert isinstance(result.paths, list)
        assert result.bytes_freed == 0
        public_result = await store.collect_garbage(GcAction.RETURN_DEAD)
        assert isinstance(public_result, GcResult)
        assert public_result.bytes_freed == 0


@NIX_GC_ROOTS_BUG
async def test_collect_garbage_delete_dead_is_hermetic(isolated_nix_environment: NixTestEnvironment) -> None:
    async with isolated_nix_environment.rpc_session() as session, session.store() as store:
        result = await store.rpc.collect_garbage(CollectGarbageRequest(action=GcAction.DELETE_DEAD, max_freed=1))
        assert isinstance(result.paths, list)


async def test_public_query_missing(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        missing = await store.query_missing(["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
        assert isinstance(missing, MissingInfo)
        assert isinstance(missing.will_build, list)
        assert isinstance(missing.will_substitute, list)
        assert isinstance(missing.unknown, list)


async def test_public_query_missing_accepts_store_path(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        missing = await store.query_missing([seeded_store_path])
        assert isinstance(missing, MissingInfo)


async def test_add_perm_root_and_indirect_root(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "nanopynix-gc-root"
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        response = await store.rpc.add_perm_root(
            AddPermRootRequest(store_path=str(seeded_store_path), gc_root=str(root_path))
        )
        assert response.path == str(root_path)
        assert root_path.is_symlink()
        await store.rpc.add_indirect_root(AddIndirectRootRequest(path=str(root_path)))


async def test_ensure_path(
    shared_nix_environment: NixTestEnvironment,
    seeded_store_path: StorePath,
) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        await store.rpc.ensure_path(EnsurePathRequest(path=str(seeded_store_path)))


async def test_optimise_and_verify_store(shared_nix_environment: NixTestEnvironment) -> None:
    async with shared_nix_environment.rpc_session() as session, session.store() as store:
        await store.rpc.optimise_store(OptimiseStoreRequest())
        response = await store.rpc.verify_store(VerifyStoreRequest(check_contents=False, repair=False))
        assert response.errors is False
