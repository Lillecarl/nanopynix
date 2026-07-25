"""Tests for the asynchronous direct-pointer in-process API."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false
# nanopynix_store / nanopynix_expr are C++ extensions without type stubs.

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from anyio import Path as AnyioPath
from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import util as nanopynix_util
from nanopynix_proto.nix.store import GcAction

import nanopynix
from nanopynix import Derivation, GcResult, MissingInfo, NixType, StorePath, inproc, yaml_primops
from nanopynix.inproc import _impl as inproc_impl
from nanopynix.settings import NixEvalSettings, normalize_nix_path
from tests.support.git import init_flake_repo
from tests.support.nix_markers import NIX_GC_ROOTS_BUG

if TYPE_CHECKING:
    from tests.support.nix_environment import InprocSessionFactory, NixTestEnvironment

requires_dynamic_primops = pytest.mark.nix_capability("dynamic_primop_registration")


@pytest.mark.anyio
@requires_dynamic_primops
async def test_inproc_yaml_primops(inproc_session: InprocSessionFactory) -> None:
    """nanopynix.primops' bundled specs (yaml_primops() here) register the
    same way for inproc.Session as they already do for rpc.Session -- see
    inproc.Session's primops= kwarg."""
    async with (
        inproc_session(primops=yaml_primops()) as nix,
        nix.store() as store,
        nix.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML "apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: demo\\n"')
        assert await parsed.to_python() == {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo"},
        }

        rendered = await eval.string(
            'builtins.toYAML { apiVersion = "v1"; kind = "ConfigMap"; metadata.name = "demo"; }',
        )
        text = await rendered.to_python()
        assert isinstance(text, str)
        assert "apiVersion: v1" in text
        assert "kind: ConfigMap" in text
        assert "name: demo" in text


@pytest.mark.anyio
async def test_inproc_eval_value_navigation(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        root = await eval.string('{ greeting = "hello"; numbers = [ 1 2 3 ]; }')
        assert await (await root.attr("greeting")).as_string() == "hello"
        numbers = await root.attr("numbers")
        assert await numbers.list_length() == 3
        assert await (await numbers.list_get(1)).as_int() == 2
        assert await root.has_attr("greeting")
        assert not await root.has_attr("missing")


@pytest.mark.anyio
async def test_inproc_value_autocall_and_realise_argv(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        function = await eval.string("x: x + 1")
        assert await (await function.call(41)).as_int() == 42
        argv = await eval.string('[ "echo" "hello" ]')
        assert await argv.realise_argv() == ["echo", "hello"]


@pytest.mark.anyio
async def test_inproc_repl_supports_shared_protocol_operations(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        repl = await eval.repl()
        assert await repl.line("answer = 42") is None
        value = await repl.line("answer")
        if value is None:
            raise AssertionError("REPL expression unexpectedly created a binding")
        assert await value.as_int() == 42
        assert "answer" in await repl.scope_names()
        await repl.reset_file_cache()


@pytest.mark.anyio
@pytest.mark.concurrency
async def test_inproc_allows_concurrent_eval_states_on_one_store(inproc_session: InprocSessionFactory) -> None:
    """Two independent EvalSessions may be open on the same Store at once."""
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as first:
        second = nix.eval(store)
        await second.open()
        try:
            assert await (await first.string("40 + 2")).as_int() == 42
            assert await (await second.string("41 + 1")).as_int() == 42
        finally:
            await second.close()


@pytest.mark.concurrency
async def test_inproc_concurrent_eval_sessions_have_independent_pure_eval(
    inproc_session: InprocSessionFactory,
) -> None:
    """Concurrently open EvalSessions may disagree on pure_eval.

    Each EvalSession owns its own EvalState, constructed with its own
    NixEvalSettings — settings are not applied through shared process state.
    """
    async with inproc_session() as nix, nix.store() as store:
        pure = nix.eval(store, eval_settings=NixEvalSettings(pure_eval=True))
        impure = nix.eval(store, eval_settings=NixEvalSettings(pure_eval=False))
        await pure.open()
        await impure.open()
        try:
            # nanopynix.EvalError, not the raw nanobind nanopynix_expr.EvalError:
            # inproc translates Nix binding exceptions onto the public hierarchy
            # at its call chokepoint, so both engines raise the same type for
            # the same failure. tests/nanopynix/rpc/client/test_pure_eval.py
            # asserts the identical expectation for the rpc engine.
            with pytest.raises(nanopynix.EvalError, match="currentTime"):
                await pure.string("builtins.currentTime")
            assert await (await impure.string("builtins.currentTime")).as_int() > 0
        finally:
            await pure.close()
            await impure.close()


@pytest.mark.anyio
async def test_inproc_value_rejects_use_after_eval_close(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        eval = nix.eval(store)
        await eval.open()
        value = await eval.string("1")
        await eval.close()
        with pytest.raises(inproc.InprocSessionClosedError):
            await value.get_type()


@pytest.mark.anyio
async def test_inproc_value_context_manager_releases_rooted_value(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        async with await eval.string("{ answer = 42; }") as root:
            assert await (await root.attr("answer")).as_int() == 42
        with pytest.raises(inproc.InprocValueReleasedError):
            await root.get_type()


@pytest.mark.anyio
async def test_inproc_eval_close_releases_values_left_open(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        eval = nix.eval(store)
        await eval.open()
        value = await eval.string("1")
        await eval.close()
        with pytest.raises(inproc.InprocSessionClosedError):
            await value.get_type()


@pytest.mark.anyio
async def test_inproc_eval_state_can_be_closed_and_reopened(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
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
async def test_inproc_store_cannot_close_while_its_eval_state_is_open(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        eval = nix.eval(store)
        await eval.open()
        with pytest.raises(RuntimeError, match="close the EvalSession first"):
            await store.close()
        await store.close(force=True)
        with pytest.raises(inproc.InprocSessionClosedError):
            await eval.string("1")


@pytest.mark.anyio
async def test_inproc_locked_flake_facade(tmp_path: Path, inproc_session: InprocSessionFactory) -> None:
    init_flake_repo(tmp_path, "value = 42;")

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
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
async def test_inproc_store_query_missing(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        mi = await store.query_missing(
            ["/nix/store/00000000000000000000000000000000-nonexistent-1.0"],
        )
        assert isinstance(mi, MissingInfo)
        assert isinstance(mi.will_build, list)
        assert isinstance(mi.will_substitute, list)
        assert isinstance(mi.unknown, list)


@pytest.mark.anyio
async def test_inproc_store_read_derivation(inproc_session: InprocSessionFactory) -> None:
    """read_derivation via direct string argument."""
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        drv_value = await eval.string(
            '(builtins.derivation { name = "inproc-read-derivation"; system = builtins.currentSystem; builder = "/bin/sh"; }).drvPath',
        )
        drv = StorePath(await drv_value.as_string())
        d = await store.read_derivation(str(drv))
        assert isinstance(d, Derivation)
        assert d.name == "inproc-read-derivation"
        assert d.system


@pytest.mark.anyio
async def test_inproc_store_collect_garbage_return_dead(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        result = await store.collect_garbage(GcAction.RETURN_DEAD)
        assert isinstance(result, GcResult)
        assert isinstance(result.paths, list)
        assert result.bytes_freed == 0


# ── GC roots ────────────────────────────────────────────────────────────
# The four root methods reached inproc late: rpc had them from the start and
# tests/nanopynix/test_engine_parity.py carried them as rpc-only DEFECTs. What
# is exercised here is the capability itself, not the plumbing -- an
# application's whole reason for making a root is that the collector then
# refuses to take the path.


@pytest.mark.anyio
async def test_inproc_add_perm_root_makes_a_symlink_into_the_store(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "inproc-gc-root"
    async with inproc_session() as nix, nix.store() as store:
        resolved = await store.add_perm_root(seeded_store_path, str(root_path))
        assert resolved == str(root_path)
        assert root_path.is_symlink()
        assert root_path.readlink() == Path(seeded_store_path)
        # add_perm_root already registered the indirect half; calling it
        # directly on the same symlink is the documented low-level entry point
        # and must stay idempotent.
        await store.add_indirect_root(str(root_path))


@pytest.mark.anyio
@NIX_GC_ROOTS_BUG
async def test_inproc_perm_root_appears_in_find_roots(
    isolated_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """A root you just made is a root the collector can see."""
    source = tmp_path / "find-roots-fixture.txt"
    source.write_text("nanopynix find_roots fixture\n", encoding="utf-8")
    root_path = tmp_path / "inproc-find-roots-gc-root"
    async with isolated_nix_environment.rpc_session() as rpc_nix, rpc_nix.store() as rpc_store:
        seeded = await rpc_store.add_to_store(str(source), name="inproc-find-roots", method="flat")

    async with isolated_nix_environment.inproc_session() as nix, nix.store() as store:
        await store.add_perm_root(seeded, str(root_path))
        roots = await store.find_roots()
        assert [root.path for root in roots if root.link == str(root_path)] == [str(seeded)]


@pytest.mark.anyio
@NIX_GC_ROOTS_BUG
async def test_inproc_temp_root_survives_a_delete_dead_pass(
    isolated_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    """The property applications depend on: a temp root beats the collector.

    Isolated, not shared: this really does delete every unrooted path in the
    store it runs against. The control half matters as much as the assertion
    -- an unrooted sibling seeded the same way must actually be collected, or
    a store that simply collected nothing would pass this vacuously.
    """
    rooted_source = tmp_path / "temp-root-keep.txt"
    rooted_source.write_text("nanopynix temp-root keep\n", encoding="utf-8")
    doomed_source = tmp_path / "temp-root-drop.txt"
    doomed_source.write_text("nanopynix temp-root drop\n", encoding="utf-8")

    async with isolated_nix_environment.rpc_session() as rpc_nix, rpc_nix.store() as rpc_store:
        kept = await rpc_store.add_to_store(str(rooted_source), name="inproc-temp-root-keep", method="flat")
        doomed = await rpc_store.add_to_store(str(doomed_source), name="inproc-temp-root-drop", method="flat")

    async with isolated_nix_environment.inproc_session() as nix, nix.store() as store:
        await store.add_temp_root(kept)
        await store.collect_garbage(GcAction.DELETE_DEAD)
        assert await store.is_valid_path(kept)
        assert not await store.is_valid_path(doomed)


# ── Store query methods against a seeded hermetic store ─────────────────


@pytest.mark.anyio
async def test_inproc_store_uri_and_store_dir(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        uri = await store.uri()
        assert isinstance(uri, str)
        assert await store.store_dir() == "/nix/store"


@pytest.mark.anyio
async def test_inproc_store_parse_and_is_valid_path(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        parsed = await store.parse_store_path(str(seeded_store_path))
        assert str(parsed) == str(seeded_store_path)
        assert await store.is_valid_path(parsed)


@pytest.mark.anyio
async def test_inproc_store_compute_fs_closure(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        closure = await store.compute_fs_closure(seeded_store_path)
        assert isinstance(closure, list)
        assert str(seeded_store_path) in {str(p) for p in closure}


@pytest.mark.anyio
async def test_inproc_store_query_derivation_outputs_and_valid_derivers(
    inproc_session: InprocSessionFactory,
) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        drv_value = await eval.string(
            '(builtins.derivation { name = "inproc-derivation-outputs"; system = builtins.currentSystem; builder = "/bin/sh"; }).drvPath',
        )
        drv = StorePath(await drv_value.as_string())
        outputs = await store.query_derivation_outputs(drv)
        assert isinstance(outputs, list)
        for output in outputs:
            derivers = await store.query_valid_derivers(output)
            assert isinstance(derivers, list)


@pytest.mark.anyio
async def test_inproc_store_query_referrers_and_substitutable_paths(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        referrers = await store.query_referrers(seeded_store_path)
        assert isinstance(referrers, list)
        substitutable = await store.query_substitutable_paths([seeded_store_path])
        assert isinstance(substitutable, list)


@pytest.mark.anyio
async def test_inproc_store_follow_links_to_store_path(
    tmp_path: Path,
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        target = str(seeded_store_path)
        link = tmp_path / "store-path"
        link.symlink_to(target)
        resolved = await store.follow_links_to_store_path(str(link))
        assert str(resolved) == target


@pytest.mark.anyio
async def test_inproc_store_query_path_from_hash_part(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        hash_part = seeded_store_path.hash_part

        resolved = await store.query_path_from_hash_part(hash_part)
        assert resolved is not None
        assert str(resolved) == str(seeded_store_path)

        missing = await store.query_path_from_hash_part("0" * 32)
        assert missing is None


@pytest.mark.anyio
async def test_inproc_store_call_generic_l1_method(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        uri = await store.call("get_uri")
        assert isinstance(uri, str)


# ── EvalSession: file, lock_flake variants, eval_flake ──────────────────


@pytest.mark.anyio
async def test_inproc_eval_file(tmp_path: Path, inproc_session: InprocSessionFactory) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ a = 1; b = "hello"; }')

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        root = await eval.file(str(nix_file))
        assert await root.to_python() == {"a": 1, "b": "hello"}


@pytest.mark.anyio
async def test_inproc_eval_flake(tmp_path: Path, inproc_session: InprocSessionFactory) -> None:
    init_flake_repo(tmp_path, 'greeting = "hello"; count = 42;')

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        outputs = await eval.eval_flake(str(tmp_path), write_lock_file=False)
        assert await (await outputs.attr("greeting")).as_string() == "hello"
        assert await (await outputs.attr("count")).as_int() == 42
        assert not (tmp_path / "flake.lock").exists()


@pytest.mark.anyio
async def test_inproc_lock_flake_update_inputs_variants(
    tmp_path: Path,
    inproc_session: InprocSessionFactory,
) -> None:
    init_flake_repo(tmp_path, "x = 1;")

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        locked_all = await eval.lock_flake(str(tmp_path), update_inputs=True, write_lock_file=False)
        assert isinstance(locked_all.description, str)
        await locked_all.release()

        locked_specific = await eval.lock_flake(
            str(tmp_path),
            update_inputs=["nonexistent"],
            write_lock_file=False,
        )
        assert isinstance(locked_specific.description, str)
        await locked_specific.release()


# ── ReplSession: load_file, add_attrs ────────────────────────────────────


@pytest.mark.anyio
async def test_inproc_repl_load_file_and_add_attrs(tmp_path: Path, inproc_session: InprocSessionFactory) -> None:
    nix_file = tmp_path / "scope.nix"
    nix_file.write_text("{ answer = 42; }")

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        repl = await eval.repl()
        loaded = await repl.load_file(str(nix_file))
        assert await repl.add_attrs(loaded) == ["answer"]
        value = await repl.line("answer")
        if value is None:
            raise AssertionError("REPL expression unexpectedly created a binding")
        assert await value.as_int() == 42


# ── Value: scalar conversions, json, build, release ──────────────────────


@pytest.mark.anyio
async def test_inproc_value_scalar_conversions(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        assert await (await eval.string("42")).get_type() == NixType.INT
        assert await (await eval.string("3.5")).as_float() == 3.5
        assert await (await eval.string("true")).as_bool() is True
        assert await (await eval.string('"hello"')).as_string() == "hello"
        assert await (await eval.string('"realised"')).realise_string() == "realised"


@pytest.mark.anyio
async def test_inproc_value_to_python(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        deep = await eval.string("{ a = { b = 1; }; }")
        assert await deep.to_python() == {"a": {"b": 1}}

        as_json = await eval.string('{ a = 1; b = [ "x" "y" ]; }')
        assert await as_json.to_python() == {"a": 1, "b": ["x", "y"]}


@pytest.mark.anyio
async def test_inproc_value_edit_location(tmp_path: Path, inproc_session: InprocSessionFactory) -> None:
    nix_file = tmp_path / "function.nix"
    nix_file.write_text("argument: argument\n")

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        value = await eval.file(str(nix_file))
        path, line = await value.edit_location()
        assert Path(path) == nix_file
        assert line == 1


@pytest.mark.anyio
async def test_inproc_value_auto_call(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        function = await eval.string("{ x ? 1 }: x + 1")
        result = await function.auto_call()
        assert await result.as_int() == 2


@pytest.mark.anyio
async def test_inproc_value_build_and_release(
    inproc_session: InprocSessionFactory,
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        drv = await eval.string("""
            builtins.derivation {
              name = "inproc-value-build-test";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "echo built-via-inproc > $out" ];
            }
        """)
        outputs = await drv.build()
        # Nix reports the logical StorePath. The fixture's LocalStore maps it
        # beneath its private root rather than the host's /nix/store mount.
        out_path = AnyioPath(shared_nix_environment.root / "nix" / "store" / Path(outputs["out"]).name)
        assert await out_path.read_text() == "built-via-inproc\n"

        await drv.release()
        with pytest.raises(inproc.InprocValueReleasedError):
            await drv.get_type()


@pytest.mark.anyio
async def test_inproc_value_release_is_idempotent(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        value = await eval.string("1")
        await value.release()
        await value.release()


# ── Session lifecycle and error branches ─────────────────────────────────


def test_inproc_session_nix_conf_must_be_a_path() -> None:
    with pytest.raises(TypeError):
        inproc.Session(nix_conf="not-a-path")  # type: ignore[arg-type] -- exercising the runtime guard for untyped callers


def test_inproc_session_nix_conf_must_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inproc.Session(nix_conf=tmp_path / "missing.conf")


@pytest.mark.anyio
@pytest.mark.concurrency
async def test_inproc_session_rejects_second_concurrent_session(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session():
        with pytest.raises(RuntimeError, match="only one"):
            await inproc_session().open()


@pytest.mark.anyio
async def test_inproc_session_rejects_mismatched_reinitialization(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session():
        pass
    with pytest.raises(RuntimeError, match="already initialized"):
        async with inproc_session(verbosity="debug"):
            pass


@pytest.mark.anyio
async def test_inproc_session_open_and_close_are_idempotent(inproc_session: InprocSessionFactory) -> None:
    session = inproc_session()
    await session.open()
    await session.open()
    await session.close()
    await session.close()


@pytest.mark.anyio
async def test_inproc_session_owns_and_shuts_down_its_nix_thread(inproc_session: InprocSessionFactory) -> None:
    first = inproc_session()
    await first.open()
    first_executor = first._executor  # type: ignore[reportPrivateUsage] -- verifies Session owns the executor lifecycle
    first_thread = await first.run(threading.get_ident)
    await first.close()

    if first_executor is None:
        raise AssertionError("Session did not create a Nix executor")
    assert first_executor.closed
    assert first._executor is None  # type: ignore[reportPrivateUsage] -- verifies close releases Session ownership

    second = inproc_session()
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
async def test_inproc_session_verbosity_roundtrip(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix:
        original = await nix.get_verbosity()
        try:
            new_level = 3 if original != 3 else 4
            result = await nix.set_verbosity(new_level)
            assert result == new_level
            assert await nix.get_verbosity() == new_level
        finally:
            await nix.set_verbosity(original)


@pytest.mark.anyio
async def test_inproc_session_subscribe_receives_log_events(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix:
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
async def test_inproc_session_log_stream_yields_events(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix:
        stream = nix.log_stream()
        nanopynix_util._log_test("inproc log_stream test")  # type: ignore[reportPrivateUsage] -- test imports private helper
        event = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert event.message == "inproc log_stream test"
        await stream.aclose()


@pytest.mark.anyio
async def test_inproc_session_subscribe_receives_a_request_finalized_event(
    inproc_session: InprocSessionFactory,
) -> None:
    async with inproc_session() as nix:
        events: list[Any] = []
        subscription = nix.subscribe(events.append)
        try:
            await nix.get_verbosity()
            for _ in range(50):
                if any(event.is_request_finalized for event in events):
                    break
                await asyncio.sleep(0.05)
            assert any(event.is_request_finalized for event in events)
        finally:
            subscription.unsubscribe()


@pytest.mark.anyio
async def test_inproc_store_not_open_raises(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix:
        store = nix.store()
        with pytest.raises(inproc.InprocSessionClosedError):
            await store.uri()


@pytest.mark.anyio
async def test_inproc_eval_open_and_close_are_idempotent(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        eval = nix.eval(store)
        await eval.open()
        await eval.open()  # already active: no-op, does not re-raise
        await eval.close()
        await eval.close()


def test_raw_gc_action_rejects_unsupported_action() -> None:
    with pytest.raises(ValueError, match="unsupported garbage-collection action"):
        inproc_impl._raw_gc_action(object())  # type: ignore[arg-type, reportPrivateUsage] -- exercising the unmapped-action guard


# ── Pure construction-time and static-method branches ────────────────────


def test_inproc_session_nix_conf_accepts_existing_path(tmp_path: Path) -> None:
    conf = tmp_path / "nix.conf"
    conf.write_text("")
    session = inproc.Session(nix_conf=conf)
    assert session is not None


def test_normalize_nix_path_str_and_list_variants() -> None:
    """``normalize_nix_path`` (nanopynix.settings) is shared by inproc.Session and rpc.Session."""
    assert normalize_nix_path("foo=/bar") == list(nanopynix_expr.parse_nix_path("foo=/bar"))
    assert normalize_nix_path(["a", "b"]) == ["a", "b"]


# ── Additional Store/EvalSession/Value/LockedFlake coverage ──────────────


@pytest.mark.anyio
async def test_inproc_store_query_path_info(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        info = await store.query_path_info(seeded_store_path)
        assert info.path == str(seeded_store_path)


@pytest.mark.anyio
async def test_inproc_store_get_build_log(
    inproc_session: InprocSessionFactory,
    seeded_store_path: StorePath,
) -> None:
    async with inproc_session() as nix, nix.store() as store:
        log = await store.get_build_log(seeded_store_path)
        assert log is None or isinstance(log, str)


@pytest.mark.anyio
async def test_inproc_store_open_is_idempotent(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix:
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
async def test_inproc_eval_rejects_store_from_different_session(inproc_session: InprocSessionFactory) -> None:
    other_session = inproc.Session(load_config=False)
    async with inproc_session() as nix, nix.store() as store:
        with pytest.raises(ValueError, match="different inproc Session"):
            other_session.eval(store)


@pytest.mark.anyio
async def test_inproc_eval_open_requires_open_store(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix:
        store = nix.store()
        with pytest.raises(inproc.InprocSessionClosedError):
            await nix.eval(store).open()


@pytest.mark.anyio
async def test_inproc_eval_repl_requires_open_eval(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        eval = nix.eval(store)
        with pytest.raises(inproc.InprocSessionClosedError):
            await eval.repl()


@pytest.mark.anyio
async def test_inproc_session_close_auto_closes_open_eval(inproc_session: InprocSessionFactory) -> None:
    session = inproc_session()
    await session.open()
    store = session.store()
    await store.open()
    eval = session.eval(store)
    await eval.open()
    value = await eval.string("1")

    await session.close()

    with pytest.raises(inproc.InprocSessionClosedError):
        await value.get_type()


@pytest.mark.anyio
async def test_inproc_eval_close_releases_leftover_locked_flakes(
    tmp_path: Path,
    inproc_session: InprocSessionFactory,
) -> None:
    init_flake_repo(tmp_path, "val = 1;")

    async with inproc_session() as nix, nix.store() as store:
        eval = nix.eval(store)
        await eval.open()
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        await eval.close()

        assert locked not in eval._locked_flakes  # type: ignore[reportPrivateUsage] -- verifying close() drained the leftover locked flake
        with pytest.raises(inproc.InprocSessionClosedError):
            await locked.eval()


@pytest.mark.anyio
async def test_inproc_locked_flake_release_is_idempotent(
    tmp_path: Path,
    inproc_session: InprocSessionFactory,
) -> None:
    init_flake_repo(tmp_path, "val = 1;")

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        await locked.release()
        await locked.release()


@pytest.mark.anyio
async def test_inproc_value_attr_names(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        root = await eval.string("{ a = 1; b = 2; }")
        assert set(await root.attr_names()) == {"a", "b"}


@pytest.mark.anyio
async def test_inproc_value_call_accepts_value_argument(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        function = await eval.string("x: x + 1")
        argument = await eval.string("41")
        result = await function.call(argument)
        assert await result.as_int() == 42


@pytest.mark.anyio
async def test_inproc_value_rejects_use_from_different_eval_session(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store:
        eval1 = nix.eval(store)
        await eval1.open()
        value = await eval1.string("1")
        eval2 = nix.eval(store)

        with pytest.raises(ValueError, match="different inproc EvalSession"):
            value._local_for(eval2)  # type: ignore[reportPrivateUsage] -- exercising the cross-EvalSession guard

        await eval1.close()


@pytest.mark.anyio
async def test_inproc_value_build_rejects_store_from_different_session(
    inproc_session: InprocSessionFactory,
) -> None:
    other_session = inproc.Session(load_config=False)
    foreign_store = other_session.store()

    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        drv = await eval.string("1")
        with pytest.raises(ValueError, match="different inproc Session"):
            await drv.build(store=foreign_store)


@pytest.mark.anyio
async def test_inproc_value_build_raises_on_build_failure(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as nix, nix.store() as store, nix.eval(store) as eval:
        drv = await eval.string("""
            builtins.derivation {
              name = "inproc-value-build-fail-test";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "exit 1" ];
            }
        """)
        with pytest.raises(RuntimeError):
            await drv.build()
