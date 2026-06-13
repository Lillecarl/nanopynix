"""Integration tests for the L2 Store facade via Nix manager."""

import pytest

from nanopynix import MissingInfo, Nix, PathInfo, StorePath

pytestmark = pytest.mark.asyncio


async def test_open_close():
    nix = Nix()
    await nix.open()
    assert nix.store is not None
    await nix.close()


async def test_context_manager():
    async with Nix() as nix:
        assert nix.store is not None


async def test_get_uri():
    async with Nix() as nix:
        uri = await nix.store.get_uri()
        assert isinstance(uri, str)
        assert len(uri) > 0


async def test_get_store_dir():
    async with Nix() as nix:
        d = await nix.store.get_store_dir()
        assert d == "/nix/store"


async def test_parse_store_path():
    async with Nix() as nix:
        # Use a path that actually exists
        paths = await nix.store.query_all_valid_paths()
        if paths:
            sp = await nix.store.parse_store_path(paths[0].to_string)
            assert isinstance(sp, StorePath)
            assert paths[0].to_string == sp.to_string


async def test_is_valid_path():
    async with Nix() as nix:
        valid_paths = await nix.store.query_all_valid_paths()
        if valid_paths:
            assert await nix.store.is_valid_path(valid_paths[0])


async def test_query_path_info():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            pi = await nix.store.query_path_info(paths[0])
            assert isinstance(pi, PathInfo)
            assert isinstance(pi.path, StorePath)
            assert pi.nar_size >= 0


async def test_query_path_from_hash_part():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            hp = paths[0].hash_part
            sp = await nix.store.query_path_from_hash_part(hp)
            assert isinstance(sp, StorePath)


async def test_compute_fs_closure():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            closure = await nix.store.compute_fs_closure(paths[0])
            assert isinstance(closure, list)
            assert len(closure) >= 1
            assert all(isinstance(sp, StorePath) for sp in closure)


async def test_query_missing():
    async with Nix() as nix:
        # Real store path required — won't find it, but won't crash
        mi = await nix.store.query_missing(["/nix/store/00000000000000000000000000000000-nonexistent-1.0"])
        assert isinstance(mi, MissingInfo)


async def test_query_derived_outputs():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        drvs = [p for p in paths if p.is_derivation]
        if drvs:
            outputs = await nix.store.query_derivation_outputs(drvs[0])
            assert isinstance(outputs, list)


async def test_query_valid_derivers():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            derivers = await nix.store.query_valid_derivers(paths[0])
            assert isinstance(derivers, list)


async def test_query_referrers():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            refs = await nix.store.query_referrers(paths[0])
            assert isinstance(refs, list)


async def test_query_substitutable_paths():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            subs = await nix.store.query_substitutable_paths(paths[:1])
            assert isinstance(subs, list)


async def test_follow_links_to_store_path():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            sp = await nix.store.follow_links_to_store_path("/run/current-system")
            assert isinstance(sp, StorePath)


async def test_store_path_str_and_model_roundtrip():
    """StorePath accepts a str or a StorePath model as argument."""
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            sp = paths[0]
            # Both str and StorePath should work
            assert await nix.store.is_valid_path(sp.to_string) is True
            assert await nix.store.is_valid_path(sp) is True


async def test_add_temp_root():
    async with Nix() as nix:
        paths = await nix.store.query_all_valid_paths()
        if paths:
            # Should not raise
            await nix.store.add_temp_root(paths[0])
