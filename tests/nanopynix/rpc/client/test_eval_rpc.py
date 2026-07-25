"""Tests for eval over RPC — EvalSession + ValueProxy."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownArgumentType=false
# Session / nanopynix are C++ nanobind extensions without type stubs.
# Variable types and nested function parameter types are unresolvable.

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from anyio import Path as AnyioPath

from nanopynix import (
    EvalSessionClosedError,
    NixError,
    NixType,
    NixTypeError,
    ValueReleasedError,
    strip_ansi,
    yaml_primops,
)
from tests.support.git import init_flake_repo

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment, RpcSessionFactory

requires_dynamic_primops = pytest.mark.nix_capability("dynamic_primop_registration")


async def test_force_closing_store_closes_dependent_eval_state(rpc_session: RpcSessionFactory) -> None:
    async with rpc_session() as nix:
        store = nix.store()
        await store.open()
        eval = nix.eval(store)
        await eval.open()
        value = await eval.string("1")

        await store.close(force=True)

        with pytest.raises(EvalSessionClosedError, match="EvalSession has been closed"):
            await value.force()


async def test_eval_file_simple(rpc_session: RpcSessionFactory, tmp_path: Path):
    """session.file returns a ValueProxy, to_python() resolves to Python dict."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ a = 1; b = "hello"; c = true; }')

    async with (
        rpc_session() as nix,
        nix.store() as store,
        nix.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert root.nix_type == NixType.ATTRS
        result = await root.to_python()
        assert result == {"a": 1, "b": "hello", "c": True}


async def test_eval_file_directory_loads_default_nix(rpc_session: RpcSessionFactory, tmp_path: Path):
    """A directory expression path resolves to its default.nix."""
    (tmp_path / "default.nix").write_text("42")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        assert await (await eval.file(str(tmp_path))).force() == 42


async def test_eval_file_uses_nix_lookup_path(rpc_session: RpcSessionFactory, tmp_path: Path):
    """Expression paths use Nix's ``lookupFileArg`` search-path resolver."""
    expressions = tmp_path / "expressions"
    expressions.mkdir()
    (expressions / "default.nix").write_text("42")

    async with (
        rpc_session(nix_path=[f"example={expressions}"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        assert await (await eval.file("<example>")).force() == 42


async def test_eval_attr_navigation(rpc_session: RpcSessionFactory, tmp_path: Path):
    """Navigate into an attrset via .attr(), then force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ inner = { x = 42; y = "hi"; }; }')

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        inner = root.attr("inner")
        assert await inner.get_type() == NixType.ATTRS
        x = inner.attr("x")
        assert await x.get_type() == NixType.INT
        assert await x.as_int() == 42


async def test_eval_list(rpc_session: RpcSessionFactory, tmp_path: Path):
    """session.file a list, navigate by index, force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("[ 1 2 3 ]")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert root.nix_type == NixType.LIST
        assert await root.list_length() == 3
        first = root.list_get(0)
        assert await first.get_type() == NixType.INT
        assert await first.force() == 1


async def test_eval_string(rpc_session: RpcSessionFactory):
    """session.string evaluates an inline expression."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.string("42 + 1")
        assert root.nix_type == NixType.INT
        assert await root.force() == 43


async def test_forced_attrs_borrow_the_parent_value_handle(rpc_session: RpcSessionFactory):
    """Attrs are views: only their parent owns and releases the worker handle."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parent = await eval.string("{ x = 1; }")
        attrs = await parent.as_dict()
        child = attrs["x"]

        assert not hasattr(attrs, "release")
        await parent.release()
        with pytest.raises(ValueReleasedError, match="ValueProxy has been released"):
            await child.force()


async def test_eval_realise_command_values_preserves_string_context(
    rpc_session: RpcSessionFactory,
    shared_nix_environment: NixTestEnvironment,
):
    """Realising strings and argv uses Nix's context-aware coercion path."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        string = await eval.string('builtins.toFile "nanopynix-realise-string" "contents"')
        realised_path = await string.realise_string()
        assert await AnyioPath(shared_nix_environment.physical_path(realised_path)).read_text() == "contents"

        argv_value = await eval.string('[ "echo" (builtins.toFile "nanopynix-realise-argv" "argument") ]')
        argv = await argv_value.realise_argv()
        assert argv[0] == "echo"
        assert await AnyioPath(shared_nix_environment.physical_path(argv[1])).read_text() == "argument"


async def test_repl_session_persists_bindings(rpc_session: RpcSessionFactory, tmp_path: Path):
    """ReplSession evaluates expressions and files in one persistent Nix scope."""
    nix_file = tmp_path / "uses-binding.nix"
    nix_file.write_text("x + 1")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        assert await repl.line("x = 41") is None
        assert await (await repl.string("x + 1")).force() == 42
        assert await (await repl.file(str(nix_file))).force() == 42


async def test_repl_session_scope_names_include_bindings_and_base_scope(rpc_session: RpcSessionFactory):
    """Completion can discover both REPL bindings and Nix's base identifiers."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        assert await repl.line("completionAnswer = 42") is None
        names = await repl.scope_names()

    assert "completionAnswer" in names
    assert "builtins" in names


async def test_repl_session_file_uses_nix_lookup_path(rpc_session: RpcSessionFactory, tmp_path: Path):
    """REPL file evaluation uses the same Nix path resolver as ``:load``."""
    expressions = tmp_path / "expressions"
    expressions.mkdir()
    (expressions / "default.nix").write_text("x + 1")

    async with (
        rpc_session(nix_path=[f"example={expressions}"]) as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        assert await repl.line("x = 41") is None
        assert await (await repl.file("<example>")).force() == 42


async def test_repl_session_load_file_autocalls_top_level_function(rpc_session: RpcSessionFactory, tmp_path: Path):
    """``load_file`` matches ``nix repl :load`` top-level auto-calling."""
    (tmp_path / "default.nix").write_text("{ value ? 41 }: { answer = value + 1; }")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        loaded = await repl.load_file(str(tmp_path))
        assert await loaded.attr("answer").force() == 42
        assert await repl.add_attrs(loaded) == ["answer"]
        assert await (await repl.string("answer")).force() == 42


async def test_repl_session_edit_location_uses_function_source(rpc_session: RpcSessionFactory, tmp_path: Path):
    """edit_location() resolves a lambda to its physical source file and line."""
    nix_file = tmp_path / "function.nix"
    nix_file.write_text("argument: argument\n")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        value = await repl.file(str(nix_file))
        path, line = await value.edit_location()

    assert Path(path) == nix_file
    assert line == 1


async def test_repl_session_adds_attrs_to_scope(rpc_session: RpcSessionFactory):
    """ReplSession can merge an evaluated attrset into its lexical scope."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        names = await repl.add_attrs(await repl.string("{ answer = 42; }"))
        assert names == ["answer"]
        assert await (await repl.string("answer")).force() == 42


async def test_eval_attr_names(rpc_session: RpcSessionFactory, tmp_path: Path):
    """attr_names() returns keys of an attrset (insertion order)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ z = 1; a = 2; m = 3; }")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        names = await root.attr_names()
        assert set(names) == {"a", "m", "z"}


async def test_eval_has_attr(rpc_session: RpcSessionFactory, tmp_path: Path):
    """has_attr() checks for key existence."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ foo = 1; }")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert await root.has_attr("foo") is True
        assert await root.has_attr("bar") is False


async def test_eval_force_does_not_consume(rpc_session: RpcSessionFactory, tmp_path: Path):
    """to_python() does NOT release the handle — we can force again."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        r1 = await root.to_python()
        r2 = await root.to_python()
        assert r1 == r2 == {"a": 1}


async def test_eval_session_cleanup(rpc_session: RpcSessionFactory, tmp_path: Path):
    """Handles are released when the eval session exits."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with rpc_session() as session, session.store() as store:
        async with session.eval(store) as eval:
            root = await eval.file(str(nix_file))
            await root.force()
        # Eval session closed — store is still available
        uri = await store.uri()
        assert isinstance(uri, str)


async def test_eval_thunk(rpc_session: RpcSessionFactory, tmp_path: Path):
    """session.file on a file with a thunk (lazy value)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("let x = 1 + 2; in { inherit x; }")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert root.nix_type == NixType.ATTRS
        x = root.attr("x")
        result = await x.force()
        assert result == 3


async def test_eval_nested_navigation(rpc_session: RpcSessionFactory, tmp_path: Path):
    """Deep navigation: a.b.c"""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = { b = { c = 99; }; }; }")

    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        a = root.attr("a")
        b = a.attr("b")
        c = b.attr("c")
        assert await c.force() == 99


async def test_eval_call_function(rpc_session: RpcSessionFactory):
    """ValueProxy.call passes JSON-compatible Python args to a Nix function."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: x + 1")
        assert await fn.get_type() == NixType.FUNCTION
        result = await fn.call(41)
        assert await result.force() == 42


async def test_eval_call_function_with_value_proxy_arg(rpc_session: RpcSessionFactory):
    """ValueProxy.call can pass an existing same-session Nix value by handle."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: x.value + 1")
        arg = await eval.string('{ value = 41; ignored = abort "not forced"; }')
        result = await fn(arg)
        assert await result.as_int() == 42


async def test_eval_reused_function_can_call_separately_evaluated_value(rpc_session: RpcSessionFactory):
    """A function proxy can be reused with other values evaluated in the same session."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: y: x + y")
        left = await eval.string("20 + 1")
        right = await eval.string("20 + 1")

        partial = await fn(left)
        result = await partial(right)

        assert await result.as_int() == 42


async def test_eval_call_function_with_nested_value_proxy_arg(rpc_session: RpcSessionFactory):
    """ValueProxy.call can pass same-session Nix values inside copied containers."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: (builtins.elemAt x.items 0).value + builtins.elemAt x.items 1")
        arg = await eval.string('{ value = 40; ignored = abort "not forced"; }')
        result = await fn({"items": [arg, 2]})
        assert await result.as_int() == 42


async def test_eval_callable_function_proxy(rpc_session: RpcSessionFactory):
    """ValueProxy is directly callable when it contains a Nix function."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: x.name")
        result = await fn({"name": "demo"})
        assert await result.as_string() == "demo"


async def test_eval_auto_call_function_with_default_attrset_args(rpc_session: RpcSessionFactory):
    """auto_call applies Nix top-level call semantics for all-default attrset lambdas."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string('{ pkgs ? {}, name ? "demo" }: { inherit name pkgs; }')
        result = await fn.auto_call()

        assert await result.attr("name").as_string() == "demo"
        assert await result.has_attr("pkgs")


async def test_eval_call_non_function_raises(rpc_session: RpcSessionFactory):
    """Calling a non-function checks the remote type before issuing call RPC."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        value = await eval.string("42")
        # Nix's own message, from the worker -- the proxy no longer pre-checks
        # the type client-side, so this is the same NixTypeError inproc raises.
        with pytest.raises(NixTypeError, match="not a function"):
            await value(1)


async def test_eval_strict_accessor_and_apply(rpc_session: RpcSessionFactory):
    """as_* reads a value of the right type; apply() runs a Nix function on it."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        number = await eval.string("42")
        text_number = await eval.string('"42"')
        text_bad = await eval.string('"forty-two"')
        attrs = await eval.string("{ x = 1; }")

        assert await number.as_int() == 42
        with pytest.raises(NixTypeError):
            await text_number.as_int()

        # The dedicated coercion methods are gone; apply() covers them with
        # Nix's own builtins, which is where the semantics should come from.
        assert await (await number.apply("builtins.toString")).as_string() == "42"
        assert await (await text_bad.apply("builtins.stringLength")).as_int() == len("forty-two")
        with pytest.raises(NixError):
            await (await attrs.apply("builtins.toString")).as_string()


async def test_to_python_refuses_a_tree_containing_a_function(rpc_session: RpcSessionFactory):
    """The deep conversion no longer preserves function leaves -- it refuses them.

    ``force_deep`` used to return a tree in which a function leaf survived as
    a callable ValueProxy. That was rpc-only -- the in-process engine returned
    the *string* ``"function"`` for the same tree -- and it came bundled with a
    walk that died on any derivation. ``to_python`` is Nix's own
    ``printValueAsJSON``, which refuses a function outright on both engines.

    Reaching a function inside a tree is still navigation: ``attr()`` down to
    it and keep it as a value, rather than asking for the whole tree as data
    and expecting live handles to survive inside it -- which the second half
    of this test does.
    """
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.string("{ x = 1; f = y: y + 2; nested.g = z: z.name; }")

        with pytest.raises(NixError):
            await root.to_python()

        assert await root.attr("x").as_int() == 1
        f_result = await root.attr("f")(40)
        assert await f_result.as_int() == 42


async def test_evaluated_derivation_can_build_while_eval_session_is_active(
    rpc_session: RpcSessionFactory,
    shared_nix_environment: NixTestEnvironment,
):
    """ValueProxy.build builds the evaluated derivation through the eval store handle."""
    async with (
        rpc_session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        drv = await eval.string(
            """
            let
              pkgs = import <nixpkgs> {};
            in
              pkgs.stdenvNoCC.mkDerivation {
                pname = "nanopynix-build-value-test";
                version = "1";
                dontUnpack = true;
                installPhase = ''
                  echo hello > "$out"
                '';
              }
            """,
        )

        outputs = await drv.build()

        assert set(outputs) == {"out"}
        assert outputs["out"].startswith("/nix/store/")
        assert "nanopynix-build-value-test" in outputs["out"]
        assert await AnyioPath(shared_nix_environment.physical_path(outputs["out"])).read_text() == "hello\n"


async def test_evaluated_derivation_can_build_with_explicit_build_store(
    rpc_session: RpcSessionFactory,
    shared_nix_environment: NixTestEnvironment,
):
    """ValueProxy.build(store=...) uses that store for building, keeping the eval store as context."""
    async with (
        rpc_session() as session,
        session.store() as eval_store,
        session.store() as build_store,
        session.eval(eval_store) as eval,
    ):
        drv = await eval.string(
            """
            let
              pkgs = import <nixpkgs> {};
            in
              pkgs.stdenvNoCC.mkDerivation {
                pname = "nanopynix-build-value-explicit-store-test";
                version = "1";
                dontUnpack = true;
                installPhase = ''
                  echo explicit > "$out"
                '';
              }
            """,
        )

        outputs = await drv.build(store=build_store)

        assert set(outputs) == {"out"}
        assert outputs["out"].startswith("/nix/store/")
        assert "nanopynix-build-value-explicit-store-test" in outputs["out"]
        assert await AnyioPath(shared_nix_environment.physical_path(outputs["out"])).read_text() == "explicit\n"


async def test_copy_closure_between_stores(rpc_session: RpcSessionFactory, tmp_path: Path):
    """Store.copy_closure copies a path from one open store to another."""
    async with (
        rpc_session() as session,
        session.store() as source,
        session.store(uri=f"local?root={tmp_path / 'dest'}") as dest,
    ):
        source_file = tmp_path / "copy-closure-fixture.txt"
        source_file.write_text("nanopynix copy_closure fixture\n", encoding="utf-8")
        path = await source.add_to_store(str(source_file), name="nanopynix-copy-closure-fixture")

        assert not await dest.is_valid_path(path)
        await source.copy_closure([path], dest)
        assert await dest.is_valid_path(path)


@requires_dynamic_primops
async def test_worker_yaml_primops(rpc_session: RpcSessionFactory):
    """Importable worker primops parse and render YAML during eval."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
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
        text = await rendered.as_string()
        assert "apiVersion: v1" in text
        assert "kind: ConfigMap" in text
        assert "name: demo" in text


@requires_dynamic_primops
async def test_worker_yaml_primops_parse_yaml12_modes(rpc_session: RpcSessionFactory):
    """fromYAML uses modern YAML mode syntax instead of YAML 1.1 octal literals."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML "mode1: 0444\\nmode2: 0o444\\nmode3: \\"0444\\"\\n"')
        assert await parsed.to_python() == {
            "mode1": 444,
            "mode2": 292,
            "mode3": "0444",
        }


@requires_dynamic_primops
async def test_worker_yaml_primops_parse_yaml11_modes(rpc_session: RpcSessionFactory):
    """fromYAML11 keeps legacy YAML 1.1 octal parsing for old manifests."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML11 "mode: 0444\\ntruth: yes\\n"')
        assert await parsed.to_python() == {"mode": 292, "truth": True}


@requires_dynamic_primops
async def test_worker_from_yaml_root_list_is_single_document(rpc_session: RpcSessionFactory):
    """A root list is still one YAML document, not a document stream."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML "- a\\n- b\\n"')
        assert await parsed.to_python() == ["a", "b"]


@requires_dynamic_primops
async def test_worker_from_yaml_rejects_document_stream(rpc_session: RpcSessionFactory):
    """fromYAML requires exactly one document; streams use fromYAMLStream."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await eval.string('builtins.fromYAML "kind: ConfigMap\\n---\\nkind: Service\\n"')
        message = strip_ansi(str(exc_info.value))
        assert "fromYAML: expected exactly one YAML document, got 2" in message
        assert "use fromYAMLStream for multi-document YAML" in message
        assert "Python primop" not in message


@requires_dynamic_primops
async def test_worker_from_yaml_parse_error_is_descriptive(rpc_session: RpcSessionFactory):
    """YAML parse failures include builtin and source location context."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await eval.string('builtins.fromYAML "metadata:\\n  name: demo\\n  : bad\\n"')
        message = strip_ansi(str(exc_info.value))
        assert "fromYAML: failed to parse YAML 1.2 document" in message
        assert "line 3" in message
        assert "Python primop" not in message


@requires_dynamic_primops
async def test_worker_yaml_stream_primops(rpc_session: RpcSessionFactory):
    """YAML stream helpers handle Kubernetes multi-document manifests."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string(
            'builtins.fromYAMLStream "apiVersion: v1\\nkind: ConfigMap\\n---\\napiVersion: v1\\nkind: Service\\n"',
        )
        assert await parsed.to_python() == [
            {"apiVersion": "v1", "kind": "ConfigMap"},
            {"apiVersion": "v1", "kind": "Service"},
        ]

        rendered = await eval.string(
            'builtins.toYAML [ { apiVersion = "v1"; kind = "ConfigMap"; } { apiVersion = "v1"; kind = "Service"; } ]',
        )
        text = await rendered.as_string()
        assert text.count("---") == 2
        assert "kind: ConfigMap" in text
        assert "kind: Service" in text


@requires_dynamic_primops
async def test_worker_to_yaml_rejects_functions(rpc_session: RpcSessionFactory):
    """toYAML is JSON-compatible data only; nested functions must not stringify."""
    async with (
        rpc_session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await eval.string("builtins.toYAML { f = x: x; }")
        message = strip_ansi(str(exc_info.value))
        assert "toYAML: argument contains non JSON-compatible Nix value of type" in message
        assert "function" in message
        assert "Python primop" not in message


@pytest.mark.concurrency
async def test_eval_concurrent_sessions(rpc_session: RpcSessionFactory, tmp_path: Path):
    """Two concurrent eval sessions — each in its own Session."""
    f1 = tmp_path / "a.nix"
    f2 = tmp_path / "b.nix"
    f1.write_text("{ val = 10; }")
    f2.write_text("{ val = 20; }")

    async def eval_one(path: str) -> Any:
        async with (
            rpc_session() as nix,
            nix.store() as store,
            nix.eval(store) as session,
        ):
            root = await session.file(path)
            v = root.attr("val")
            return await v.force()

    results = await asyncio.gather(eval_one(str(f1)), eval_one(str(f2)))
    assert results == [10, 20]


# ── Flake evaluation over RPC ──────────────────────────────────────────


def _init_git_flake(tmp_path: Path, outputs_body: str) -> None:
    """Create a temp flake with a git repo for RPC testing."""
    init_flake_repo(tmp_path, outputs_body)


async def test_eval_flake(rpc_session: RpcSessionFactory, tmp_path: Path):
    """eval_flake locks and evaluates a flake, returns navigable outputs."""
    _init_git_flake(tmp_path, 'greeting = "hello"; count = 42;')

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        outputs = await eval.eval_flake(str(tmp_path), write_lock_file=False)
        assert outputs.nix_type == NixType.ATTRS
        greeting = outputs.attr("greeting")
        assert await greeting.force() == "hello"
        count = outputs.attr("count")
        assert await count.force() == 42


async def test_eval_flake_to_python(rpc_session: RpcSessionFactory, tmp_path: Path):
    """eval_flake + to_python on a sub-attrset serializes it to JSON."""
    _init_git_flake(tmp_path, 'lib = { name = "test"; nested = { x = 1; y = [ "a" "b" ]; }; };')

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        outputs = await eval.eval_flake(str(tmp_path), write_lock_file=False)
        lib = outputs.attr("lib")
        result = await lib.to_python()
        assert isinstance(result, dict)
        assert result["name"] == "test"
        nested = result["nested"]
        assert isinstance(nested, dict)
        assert nested["x"] == 1
        assert nested["y"] == ["a", "b"]


async def test_lock_flake_and_eval_locked(rpc_session: RpcSessionFactory, tmp_path: Path):
    """lock_flake + eval_locked_flake: in-memory lock, evaluate without writing."""
    _init_git_flake(tmp_path, "val = 99;")

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()
        assert locked.handle > 0

        outputs = await locked.eval()
        val = outputs.attr("val")
        assert await val.force() == 99


async def test_locked_flake_release_invalidates_handle(rpc_session: RpcSessionFactory, tmp_path: Path):
    """release_locked_flake marks the local handle unusable."""
    _init_git_flake(tmp_path, "val = 99;")

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        await locked.release()
        await locked.release()

        with pytest.raises(ValueReleasedError, match="LockedFlakeHandle"):
            await locked.eval()


async def test_lock_flake_write_lock_file(rpc_session: RpcSessionFactory, tmp_path: Path):
    """lock_flake with write_lock_file=False, then write_lock_file() persists."""
    _init_git_flake(tmp_path, "val = 1;")

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()

        await locked.write_lock_file()
        assert (tmp_path / "flake.lock").exists()


async def test_lock_flake_no_write_does_not_leak(rpc_session: RpcSessionFactory, tmp_path: Path):
    """lock_flake with write_lock_file=False must NOT create flake.lock."""
    _init_git_flake(tmp_path, "val = 1;")

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        await eval.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()


async def test_lock_flake_update_all(rpc_session: RpcSessionFactory, tmp_path: Path):
    """lock_flake with update_inputs=True re-resolves all inputs."""
    _init_git_flake(tmp_path, "x = 1;")

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(
            str(tmp_path),
            update_inputs=True,
            write_lock_file=False,
        )
        assert locked.handle > 0


async def test_lock_flake_update_specific_input(rpc_session: RpcSessionFactory, tmp_path: Path):
    """lock_flake with update_inputs=list re-resolves only specified inputs."""
    _init_git_flake(tmp_path, "x = 1;")

    async with (
        rpc_session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(
            str(tmp_path),
            update_inputs=["nonexistent"],
            write_lock_file=False,
        )
        assert locked.handle > 0
