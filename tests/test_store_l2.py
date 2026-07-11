"""Integration tests for the L2 Store facade via Session."""

from nanopynix import MissingInfo, PathInfo, Session, StorePath


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
        uri = await store.get_uri()
        assert isinstance(uri, str)
        assert len(uri) > 0


async def test_get_store_dir():
    async with Session() as session, session.store() as store:
        d = await store.get_store_dir()
        assert d == "/nix/store"


async def test_parse_store_path():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            sp = await store.parse_store_path(paths[0].to_string)
            assert isinstance(sp, StorePath)
            assert paths[0].to_string == sp.to_string


async def test_is_valid_path():
    async with Session() as session, session.store() as store:
        valid_paths = await store.query_all_valid_paths()
        if valid_paths:
            assert await store.is_valid_path(valid_paths[0])


async def test_query_path_info():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            pi = await store.query_path_info(paths[0])
            assert isinstance(pi, PathInfo)
            assert isinstance(pi.path, StorePath)
            assert pi.nar_size >= 0


async def test_query_path_from_hash_part():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            hp = paths[0].hash_part
            sp = await store.query_path_from_hash_part(hp)
            assert isinstance(sp, StorePath)


async def test_compute_fs_closure():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            closure = await store.compute_fs_closure(paths[0])
            assert isinstance(closure, list)
            assert len(closure) >= 1
            assert all(isinstance(sp, StorePath) for sp in closure)


async def test_query_missing():
    async with Session() as session, session.store() as store:
        mi = await store.query_missing(["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
        assert isinstance(mi, MissingInfo)


async def test_query_derived_outputs():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        drvs = [p for p in paths if p.is_derivation]
        if drvs:
            outputs = await store.query_derivation_outputs(drvs[0])
            assert isinstance(outputs, list)


async def test_query_valid_derivers():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            derivers = await store.query_valid_derivers(paths[0])
            assert isinstance(derivers, list)


async def test_query_referrers():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            refs = await store.query_referrers(paths[0])
            assert isinstance(refs, list)


async def test_query_substitutable_paths():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            subs = await store.query_substitutable_paths(paths[:1])
            assert isinstance(subs, list)


async def test_follow_links_to_store_path():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            sp = await store.follow_links_to_store_path("/run/current-system")
            assert isinstance(sp, StorePath)


async def test_store_path_str_and_model_roundtrip():
    """StorePath accepts a str or a StorePath model as argument."""
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            sp = paths[0]
            assert await store.is_valid_path(sp.to_string) is True
            assert await store.is_valid_path(sp) is True


async def test_add_temp_root():
    async with Session() as session, session.store() as store:
        paths = await store.query_all_valid_paths()
        if paths:
            await store.add_temp_root(paths[0])
