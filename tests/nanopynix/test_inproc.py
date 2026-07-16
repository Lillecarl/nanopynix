"""Tests for the asynchronous direct-pointer in-process API."""

from __future__ import annotations

from pathlib import Path

import pytest
from _git import init_flake_repo

from nanopynix import inproc


@pytest.mark.anyio
async def test_inproc_eval_value_navigation() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        root = await eval_.string('{ greeting = "hello"; numbers = [ 1 2 3 ]; }')
        assert await (await root.attr("greeting")).force() == "hello"
        numbers = await root.attr("numbers")
        assert await numbers.list_length() == 3
        assert await (await numbers.list_get(1)).force() == 2
        assert await root.has_attr("greeting")
        assert not await root.has_attr("missing")


@pytest.mark.anyio
async def test_inproc_value_autocall_and_realise_argv() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        function = await eval_.string("x: x + 1")
        assert await (await function.call(41)).as_int() == 42
        argv = await eval_.string('[ "echo" "hello" ]')
        assert await argv.realise_argv() == ["echo", "hello"]


@pytest.mark.anyio
async def test_inproc_repl_supports_shared_protocol_operations() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        repl = await eval_.repl()
        assert await repl.line("answer = 42") is None
        value = await repl.line("answer")
        if value is None:
            raise AssertionError("REPL expression unexpectedly created a binding")
        assert await value.as_int() == 42
        assert "answer" in await repl.scope_names()
        await repl.reset_file_cache()


@pytest.mark.anyio
async def test_inproc_allows_only_one_live_eval_state() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store):
        with pytest.raises(inproc.InprocEvalBusyError):
            await nix.eval(store).open()


@pytest.mark.anyio
async def test_inproc_value_rejects_use_after_eval_close() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval_ = nix.eval(store)
        await eval_.open()
        value = await eval_.string("1")
        await eval_.close()
        with pytest.raises(inproc.InprocSessionClosedError):
            await value.force()


@pytest.mark.anyio
async def test_inproc_value_context_manager_releases_rooted_value() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        async with await eval_.string("{ answer = 42; }") as root:
            assert await (await root.attr("answer")).as_int() == 42
        with pytest.raises(inproc.InprocValueReleasedError):
            await root.force()


@pytest.mark.anyio
async def test_inproc_eval_close_releases_values_left_open() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval_ = nix.eval(store)
        await eval_.open()
        value = await eval_.string("1")
        await eval_.close()
        with pytest.raises(inproc.InprocSessionClosedError):
            await value.force()


@pytest.mark.anyio
async def test_inproc_locked_flake_facade(tmp_path: Path) -> None:
    init_flake_repo(tmp_path, "value = 42;")

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        locked = await eval_.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()
        assert isinstance(locked.description, str)

        outputs = await locked.eval()
        assert await (await outputs.attr("value")).as_int() == 42

        await locked.write_lock_file()
        assert (tmp_path / "flake.lock").exists()
        await locked.release()
        with pytest.raises(inproc.InprocLockedFlakeReleasedError):
            await locked.eval()
