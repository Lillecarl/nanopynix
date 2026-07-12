"""Integration tests for the L2 Store facade via Session."""

from nanopynix import MissingInfo, PathInfo, Session, StorePath
from nanopynix_proto.nix.store import (
    AddTempRootRequest,
    ComputeFsClosureRequest,
    FollowLinksToStorePathRequest,
    GetStoreDirRequest,
    GetUriRequest,
    IsValidPathRequest,
    ParseStorePathRequest,
    QueryAllValidPathsRequest,
    QueryDerivationOutputsRequest,
    QueryMissingRequest,
    QueryPathFromHashPartRequest,
    QueryPathInfoRequest,
    QueryReferrersRequest,
    QuerySubstitutablePathsRequest,
    QueryValidDeriversRequest,
)


async def test_open_close():
    session = Session()
    await session.open()
    async with session.store() as store:
        assert store is not None
    await session.close()


async def test_context_manager():
    async with Session() as session, session.store() as store:
        assert store is not None


async def test_get_uri():
    async with Session() as session, session.store() as store:
        response = await store.get_uri(GetUriRequest())
        assert isinstance(response.uri, str)
        assert len(response.uri) > 0


async def test_get_store_dir():
    async with Session() as session, session.store() as store:
        response = await store.get_store_dir(GetStoreDirRequest())
        assert response.dir == "/nix/store"


async def test_parse_store_path():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            sp = await store.parse_store_path(ParseStorePathRequest(path=path.to_string))
            assert isinstance(sp, StorePath)
            assert path.to_string == StorePath(sp).to_string


async def test_is_valid_path():
    async with Session() as session, session.store() as store:
        valid_paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if valid_paths:
            path = StorePath(valid_paths[0])
            response = await store.is_valid_path(IsValidPathRequest(path=path.to_string))
            assert response.valid


async def test_query_path_info():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            pi = await store.query_path_info(QueryPathInfoRequest(path=path.to_string))
            assert isinstance(pi, PathInfo)
            assert isinstance(pi.path, StorePath)
            assert pi.nar_size >= 0


async def test_query_path_from_hash_part():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            hp = StorePath(paths[0]).hash_part
            response = await store.query_path_from_hash_part(QueryPathFromHashPartRequest(hash_part=hp))
            sp = response.path
            assert isinstance(sp, StorePath)


async def test_compute_fs_closure():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            closure = (await store.compute_fs_closure(ComputeFsClosureRequest(path=path.to_string))).paths
            assert isinstance(closure, list)
            assert len(closure) >= 1
            assert all(isinstance(sp, StorePath) for sp in closure)


async def test_query_missing():
    async with Session() as session, session.store() as store:
        mi = await store.query_missing(
            QueryMissingRequest(paths=["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
        )
        assert isinstance(mi, MissingInfo)


async def test_query_derived_outputs():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        drvs = [path for path in (StorePath(path) for path in paths) if path.is_derivation]
        if drvs:
            outputs = (await store.query_derivation_outputs(QueryDerivationOutputsRequest(path=drvs[0].to_string))).paths
            assert isinstance(outputs, list)


async def test_query_valid_derivers():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            derivers = (await store.query_valid_derivers(QueryValidDeriversRequest(path=path.to_string))).paths
            assert isinstance(derivers, list)


async def test_query_referrers():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            refs = (await store.query_referrers(QueryReferrersRequest(path=path.to_string))).paths
            assert isinstance(refs, list)


async def test_query_substitutable_paths():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            subs = (
                await store.query_substitutable_paths(
                    QuerySubstitutablePathsRequest(paths=[path.to_string])
                )
            ).paths
            assert isinstance(subs, list)


async def test_follow_links_to_store_path():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            sp = await store.follow_links_to_store_path(FollowLinksToStorePathRequest(path="/run/current-system"))
            assert isinstance(sp, StorePath)


async def test_store_path_str_and_model_roundtrip():
    """StorePath accepts a str or a StorePath model as argument."""
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            sp = StorePath(paths[0])
            assert StorePath(sp) is sp
            assert (await store.is_valid_path(IsValidPathRequest(path=sp.to_string))).valid is True


async def test_add_temp_root():
    async with Session() as session, session.store() as store:
        paths = (await store.query_all_valid_paths(QueryAllValidPathsRequest())).paths
        if paths:
            path = StorePath(paths[0])
            await store.add_temp_root(AddTempRootRequest(path=path.to_string))
