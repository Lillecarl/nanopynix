"""Tests for the asynchronous direct-pointer in-process API."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false
# nanopynix_store / nanopynix_expr are C++ extensions without type stubs.

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from _git import init_flake_repo
from anyio import Path as AnyioPath
from nanopynix_proto.nix.store import GcAction

import nanopynix_expr
import nanopynix_util
from nanopynix import Derivation, GcResult, MissingInfo, inproc


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
async def test_inproc_eval_state_can_be_closed_and_reopened() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        first = nix.eval(store)
        await first.open()
        local = first._local  # type: ignore[reportPrivateUsage] -- verifies the L2 evaluator pointer is released on close
        await first.close()

        if local is None:
            raise AssertionError("EvalSession did not retain its LocalEvalState")
        with pytest.raises(RuntimeError, match="local evaluator has been closed"):
            local.require_raw()

        second = nix.eval(store)
        await second.open()
        assert await (await second.string("42")).as_int() == 42
        await second.close()


@pytest.mark.anyio
async def test_inproc_store_cannot_close_while_its_eval_state_is_open() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval_ = nix.eval(store)
        await eval_.open()
        with pytest.raises(RuntimeError, match="close the EvalSession first"):
            await store.close()
        await store.close(force=True)
        with pytest.raises(inproc.InprocSessionClosedError):
            await eval_.string("1")


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


@pytest.mark.anyio
async def test_inproc_store_query_missing() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        mi = await store.query_missing(
            ["/nix/store/00000000000000000000000000000000-nonexistent-1.0"]
        )
        assert isinstance(mi, MissingInfo)
        assert isinstance(mi.will_build, list)
        assert isinstance(mi.will_substitute, list)
        assert isinstance(mi.unknown, list)


@pytest.mark.anyio
async def test_inproc_store_read_derivation() -> None:
    """read_derivation via direct string argument."""
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        drv = next((p for p in paths if p.is_derivation), None)
        if drv is not None:
            d = await store.read_derivation(str(drv))
            assert isinstance(d, Derivation)
            assert d.name
            assert d.system


@pytest.mark.anyio
async def test_inproc_store_collect_garbage_return_dead(tmp_path: Path) -> None:
    async with inproc.Session(load_config=False) as nix, nix.store(f"local?root={tmp_path}") as store:
        result = await store.collect_garbage(GcAction.RETURN_DEAD)
        assert isinstance(result, GcResult)
        assert isinstance(result.paths, list)
        assert result.bytes_freed == 0


# ── Store query methods against the real system store ──────────────────


@pytest.mark.anyio
async def test_inproc_store_uri_and_store_dir() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        uri = await store.uri()
        assert isinstance(uri, str)
        assert await store.store_dir() == "/nix/store"


@pytest.mark.anyio
async def test_inproc_store_parse_and_is_valid_path() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        path = paths[0]
        parsed = await store.parse_store_path(str(path))
        assert str(parsed) == str(path)
        assert await store.is_valid_path(parsed)


@pytest.mark.anyio
async def test_inproc_store_compute_fs_closure() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        closure = await store.compute_fs_closure(paths[0])
        assert isinstance(closure, list)
        assert str(paths[0]) in {str(p) for p in closure}


@pytest.mark.anyio
async def test_inproc_store_query_derivation_outputs_and_valid_derivers() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        drv = next((p for p in paths if p.is_derivation), None)
        if drv is None:
            pytest.skip("store has no derivations")
        outputs = await store.query_derivation_outputs(drv)
        assert isinstance(outputs, list)
        for output in outputs:
            derivers = await store.query_valid_derivers(output)
            assert isinstance(derivers, list)


@pytest.mark.anyio
async def test_inproc_store_query_referrers_and_substitutable_paths() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        path = paths[0]
        referrers = await store.query_referrers(path)
        assert isinstance(referrers, list)
        substitutable = await store.query_substitutable_paths([path])
        assert isinstance(substitutable, list)


@pytest.mark.anyio
async def test_inproc_store_follow_links_to_store_path(tmp_path: Path) -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        target = str(paths[0])
        link = tmp_path / "store-path"
        link.symlink_to(target)
        resolved = await store.follow_links_to_store_path(str(link))
        assert str(resolved) == target


@pytest.mark.anyio
async def test_inproc_store_query_path_from_hash_part() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        hash_part = str(paths[0]).removeprefix("/nix/store/").split("-", 1)[0]

        resolved = await store.query_path_from_hash_part(hash_part)
        assert resolved is not None
        assert str(resolved) == str(paths[0])

        missing = await store.query_path_from_hash_part("0" * 32)
        assert missing is None


@pytest.mark.anyio
async def test_inproc_store_call_generic_l1_method() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        uri = await store.call("get_uri")
        assert isinstance(uri, str)


# ── EvalSession: file, lock_flake variants, eval_flake ──────────────────


@pytest.mark.anyio
async def test_inproc_eval_file(tmp_path: Path) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ a = 1; b = "hello"; }')

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        root = await eval_.file(str(nix_file))
        assert await root.force() == {"a": 1, "b": "hello"}


@pytest.mark.anyio
async def test_inproc_eval_flake(tmp_path: Path) -> None:
    init_flake_repo(tmp_path, 'greeting = "hello"; count = 42;')

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        outputs = await eval_.eval_flake(str(tmp_path), write_lock_file=False)
        assert await (await outputs.attr("greeting")).as_string() == "hello"
        assert await (await outputs.attr("count")).as_int() == 42
        assert not (tmp_path / "flake.lock").exists()


@pytest.mark.anyio
async def test_inproc_lock_flake_update_inputs_variants(tmp_path: Path) -> None:
    init_flake_repo(tmp_path, "x = 1;")

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        locked_all = await eval_.lock_flake(str(tmp_path), update_inputs=True, write_lock_file=False)
        assert isinstance(locked_all.description, str)
        await locked_all.release()

        locked_specific = await eval_.lock_flake(
            str(tmp_path), update_inputs=["nonexistent"], write_lock_file=False
        )
        assert isinstance(locked_specific.description, str)
        await locked_specific.release()


# ── ReplSession: load_file, add_attrs ────────────────────────────────────


@pytest.mark.anyio
async def test_inproc_repl_load_file_and_add_attrs(tmp_path: Path) -> None:
    nix_file = tmp_path / "scope.nix"
    nix_file.write_text("{ answer = 42; }")

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        repl = await eval_.repl()
        loaded = await repl.load_file(str(nix_file))
        assert await repl.add_attrs(loaded) == ["answer"]
        value = await repl.line("answer")
        if value is None:
            raise AssertionError("REPL expression unexpectedly created a binding")
        assert await value.as_int() == 42


# ── Value: scalar conversions, json, build, release ──────────────────────


@pytest.mark.anyio
async def test_inproc_value_scalar_conversions() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        assert await (await eval_.string("42")).type() == "int"
        assert await (await eval_.string("3.5")).as_float() == 3.5
        assert await (await eval_.string("true")).as_bool() is True
        assert await (await eval_.string('"hello"')).as_string() == "hello"
        assert await (await eval_.string('"realised"')).realise_string() == "realised"


@pytest.mark.anyio
async def test_inproc_value_force_deep_and_json() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        deep = await eval_.string("{ a = { b = 1; }; }")
        assert await deep.force_deep() == {"a": {"b": 1}}

        as_json = await eval_.string('{ a = 1; b = [ "x" "y" ]; }')
        assert await as_json.json() == {"a": 1, "b": ["x", "y"]}
        assert await as_json.force_json() == {"a": 1, "b": ["x", "y"]}


@pytest.mark.anyio
async def test_inproc_value_edit_location(tmp_path: Path) -> None:
    nix_file = tmp_path / "function.nix"
    nix_file.write_text("argument: argument\n")

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        value = await eval_.file(str(nix_file))
        path, line = await value.edit_location()
        assert Path(path) == nix_file
        assert line == 1


@pytest.mark.anyio
async def test_inproc_value_auto_call() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        function = await eval_.string("{ x ? 1 }: x + 1")
        result = await function.auto_call()
        assert await result.as_int() == 2


@pytest.mark.anyio
async def test_inproc_value_build_and_release() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        drv = await eval_.string("""
            let pkgs = import <nixpkgs> {}; in
            pkgs.stdenvNoCC.mkDerivation {
              pname = "inproc-value-build-test";
              version = "1";
              dontUnpack = true;
              installPhase = ''echo built-via-inproc > "$out"'';
            }
        """)
        outputs = await drv.build()
        out_path = AnyioPath(outputs["out"])
        assert await out_path.read_text() == "built-via-inproc\n"

        await drv.release()
        with pytest.raises(inproc.InprocValueReleasedError):
            await drv.force()


@pytest.mark.anyio
async def test_inproc_value_close_is_idempotent() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        value = await eval_.string("1")
        await value.close()
        await value.close()


# ── Session lifecycle and error branches ─────────────────────────────────


def test_inproc_session_nix_conf_must_be_a_path() -> None:
    with pytest.raises(TypeError):
        inproc.Session(nix_conf="not-a-path")  # type: ignore[arg-type] -- exercising the runtime guard for untyped callers


def test_inproc_session_nix_conf_must_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inproc.Session(nix_conf=tmp_path / "missing.conf")


@pytest.mark.anyio
async def test_inproc_session_rejects_second_concurrent_session() -> None:
    async with inproc.Session(load_config=False):
        with pytest.raises(RuntimeError, match="only one"):
            await inproc.Session(load_config=False).open()


@pytest.mark.anyio
async def test_inproc_session_rejects_mismatched_reinitialization() -> None:
    async with inproc.Session(load_config=False):
        pass
    with pytest.raises(RuntimeError, match="already initialized"):
        async with inproc.Session(load_config=False, verbosity="debug"):
            pass


@pytest.mark.anyio
async def test_inproc_session_open_and_close_are_idempotent() -> None:
    session = inproc.Session(load_config=False)
    await session.open()
    await session.open()
    await session.close()
    await session.close()


@pytest.mark.anyio
async def test_inproc_session_owns_and_shuts_down_its_nix_thread() -> None:
    first = inproc.Session(load_config=False)
    await first.open()
    first_executor = first._executor  # type: ignore[reportPrivateUsage] -- verifies Session owns the executor lifecycle
    first_thread = await first.run(threading.get_ident)
    await first.close()

    if first_executor is None:
        raise AssertionError("Session did not create a Nix executor")
    assert first_executor.closed
    assert first._executor is None  # type: ignore[reportPrivateUsage] -- verifies close releases Session ownership

    second = inproc.Session(load_config=False)
    await second.open()
    second_executor = second._executor  # type: ignore[reportPrivateUsage] -- verifies a later Session gets a fresh executor
    second_thread = await second.run(threading.get_ident)
    await second.close()

    if second_executor is None:
        raise AssertionError("second Session did not create a Nix executor")
    assert second_executor is not first_executor
    assert second_executor.closed
    # Thread identifiers are allowed to be reused by the OS. The distinct
    # executor instances and their completed shutdowns establish the lifecycle.
    assert isinstance(first_thread, int)
    assert isinstance(second_thread, int)


@pytest.mark.anyio
async def test_inproc_session_verbosity_roundtrip() -> None:
    async with inproc.Session(load_config=False) as nix:
        original = await nix.get_verbosity()
        try:
            new_level = 3 if original != 3 else 4
            result = await nix.set_verbosity(new_level)
            assert result == new_level
            assert await nix.get_verbosity() == new_level
        finally:
            await nix.set_verbosity(original)


@pytest.mark.anyio
async def test_inproc_session_subscribe_receives_log_events() -> None:
    async with inproc.Session(load_config=False) as nix:
        events: list[Any] = []
        subscription = nix.subscribe(events.append)
        try:
            nanopynix_util._log_test("inproc subscribe test")  # type: ignore[reportPrivateUsage] -- test imports private helper
            for _ in range(50):
                if events:
                    break
                await asyncio.sleep(0.05)
            assert any(event.message == "inproc subscribe test" for event in events)
        finally:
            subscription.unsubscribe()


@pytest.mark.anyio
async def test_inproc_session_log_stream_yields_events() -> None:
    async with inproc.Session(load_config=False) as nix:
        stream = nix.log_stream()
        nanopynix_util._log_test("inproc log_stream test")  # type: ignore[reportPrivateUsage] -- test imports private helper
        event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert event.message == "inproc log_stream test"
        await stream.aclose()


@pytest.mark.anyio
async def test_inproc_store_not_open_raises() -> None:
    async with inproc.Session(load_config=False) as nix:
        store = nix.store()
        with pytest.raises(inproc.InprocSessionClosedError):
            await store.uri()


@pytest.mark.anyio
async def test_inproc_eval_busy_error_message_and_close_idempotent() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval_ = nix.eval(store)
        await eval_.open()
        await eval_.open()  # already active: no-op, does not re-raise
        with pytest.raises(inproc.InprocEvalBusyError):
            await nix.eval(store).open()
        await eval_.close()
        await eval_.close()


def test_raw_gc_action_rejects_unsupported_action() -> None:
    with pytest.raises(ValueError, match="unsupported garbage-collection action"):
        inproc._raw_gc_action(object())  # type: ignore[arg-type, reportPrivateUsage] -- exercising the unmapped-action guard


# ── Pure construction-time and static-method branches ────────────────────


def test_inproc_session_nix_conf_accepts_existing_path(tmp_path: Path) -> None:
    conf = tmp_path / "nix.conf"
    conf.write_text("")
    session = inproc.Session(nix_conf=conf)
    assert session is not None


def test_inproc_normalize_nix_path_str_and_list_variants() -> None:
    assert inproc.Session._normalize_nix_path("foo=/bar") == list(  # type: ignore[reportPrivateUsage] -- exercising the str branch directly
        nanopynix_expr.parse_nix_path("foo=/bar")
    )
    assert inproc.Session._normalize_nix_path(["a", "b"]) == ["a", "b"]  # type: ignore[reportPrivateUsage] -- exercising the sequence branch directly


# ── Additional Store/EvalSession/Value/LockedFlake coverage ──────────────


@pytest.mark.anyio
async def test_inproc_store_query_path_info() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        info = await store.query_path_info(paths[0])
        assert info.path == str(paths[0])


@pytest.mark.anyio
async def test_inproc_store_get_build_log() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        paths = await store.query_all_valid_paths()
        if not paths:
            pytest.skip("store has no valid paths")
        log = await store.get_build_log(paths[0])
        assert log is None or isinstance(log, str)


@pytest.mark.anyio
async def test_inproc_store_open_is_idempotent() -> None:
    async with inproc.Session(load_config=False) as nix:
        store = nix.store()
        await store.open()
        await store.open()
        await store.close()


@pytest.mark.anyio
async def test_inproc_session_run_before_open_raises() -> None:
    session = inproc.Session(load_config=False)
    with pytest.raises(inproc.InprocSessionClosedError):
        await session.run(lambda: None)


@pytest.mark.anyio
async def test_inproc_eval_rejects_store_from_different_session() -> None:
    other_session = inproc.Session(load_config=False)
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        with pytest.raises(ValueError, match="different inproc Session"):
            other_session.eval(store)


@pytest.mark.anyio
async def test_inproc_eval_open_requires_open_store() -> None:
    async with inproc.Session(load_config=False) as nix:
        store = nix.store()
        with pytest.raises(inproc.InprocSessionClosedError):
            await nix.eval(store).open()


@pytest.mark.anyio
async def test_inproc_eval_repl_requires_open_eval() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval_ = nix.eval(store)
        with pytest.raises(inproc.InprocSessionClosedError):
            await eval_.repl()


@pytest.mark.anyio
async def test_inproc_session_close_auto_closes_open_eval() -> None:
    session = inproc.Session(load_config=False)
    await session.open()
    store = session.store()
    await store.open()
    eval_ = session.eval(store)
    await eval_.open()
    value = await eval_.string("1")

    await session.close()

    with pytest.raises(inproc.InprocSessionClosedError):
        await value.force()


@pytest.mark.anyio
async def test_inproc_eval_close_releases_leftover_locked_flakes(tmp_path: Path) -> None:
    init_flake_repo(tmp_path, "val = 1;")

    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval_ = nix.eval(store)
        await eval_.open()
        locked = await eval_.lock_flake(str(tmp_path), write_lock_file=False)
        await eval_.close()

        assert locked not in eval_._locked_flakes  # type: ignore[reportPrivateUsage] -- verifying close() drained the leftover locked flake
        with pytest.raises(inproc.InprocSessionClosedError):
            await locked.eval()


@pytest.mark.anyio
async def test_inproc_locked_flake_release_is_idempotent(tmp_path: Path) -> None:
    init_flake_repo(tmp_path, "val = 1;")

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        locked = await eval_.lock_flake(str(tmp_path), write_lock_file=False)
        await locked.release()
        await locked.release()


@pytest.mark.anyio
async def test_inproc_value_attr_names() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        root = await eval_.string("{ a = 1; b = 2; }")
        assert set(await root.attr_names()) == {"a", "b"}


@pytest.mark.anyio
async def test_inproc_value_call_accepts_value_argument() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        function = await eval_.string("x: x + 1")
        argument = await eval_.string("41")
        result = await function.call(argument)
        assert await result.as_int() == 42


@pytest.mark.anyio
async def test_inproc_value_rejects_use_from_different_eval_session() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store:
        eval1 = nix.eval(store)
        await eval1.open()
        value = await eval1.string("1")
        eval2 = nix.eval(store)

        with pytest.raises(ValueError, match="different inproc EvalSession"):
            value._local_for(eval2)  # type: ignore[reportPrivateUsage] -- exercising the cross-EvalSession guard

        await eval1.close()


@pytest.mark.anyio
async def test_inproc_value_build_rejects_store_from_different_session() -> None:
    other_session = inproc.Session(load_config=False)
    foreign_store = other_session.store()

    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        drv = await eval_.string("1")
        with pytest.raises(ValueError, match="different inproc Session"):
            await drv.build(store=foreign_store)


@pytest.mark.anyio
async def test_inproc_value_build_raises_on_build_failure() -> None:
    async with inproc.Session(load_config=False) as nix, nix.store() as store, nix.eval(store) as eval_:
        drv = await eval_.string("""
            let pkgs = import <nixpkgs> {}; in
            pkgs.stdenvNoCC.mkDerivation {
              pname = "inproc-value-build-fail-test";
              version = "1";
              dontUnpack = true;
              installPhase = "exit 1";
            }
        """)
        with pytest.raises(RuntimeError):
            await drv.build()
