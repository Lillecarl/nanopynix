"""Tests for eval over RPC — EvalSession + ValueProxy."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false
# Session / nanopynix are C++ nanobind extensions without type stubs.
# Variable types and nested function parameter types are unresolvable.

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _git import init_flake_repo
from anyio import Path as AnyioPath
from nanopynix_proto.nix.store import GetUriRequest

from nanopynix import (
    NixCoercionError,
    NixType,
    Session,
    ValueProxy,
    ValueReleasedError,
    WrongNixTypeError,
    strip_ansi,
    yaml_primops,
)

requires_dynamic_primops = pytest.mark.required_nix_version("2.32", None)


async def test_eval_file_simple(tmp_path: Path):
    """session.file returns a ValueProxy, force_deep() resolves to Python dict."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ a = 1; b = "hello"; c = true; }')

    async with (
        Session() as nix,
        nix.store() as store,
        nix.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert root.nix_type == NixType.ATTRS
        result = await root.force_deep()
        assert result == {"a": 1, "b": "hello", "c": True}


async def test_eval_attr_navigation(tmp_path: Path):
    """Navigate into an attrset via .attr(), then force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ inner = { x = 42; y = "hi"; }; }')

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        inner = root.attr("inner")
        assert await inner.get_type() == NixType.ATTRS
        x = inner.attr("x")
        assert await x.get_type() == NixType.INT
        assert await x.force_as(NixType.INT) == 42


async def test_eval_list(tmp_path: Path):
    """session.file a list, navigate by index, force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("[ 1 2 3 ]")

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert root.nix_type == NixType.LIST
        assert await root.list_length() == 3
        first = root.list_get(0)
        assert await first.get_type() == NixType.INT
        assert await first.force() == 1


async def test_eval_string():
    """session.string evaluates an inline expression."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.string("42 + 1")
        assert root.nix_type == NixType.INT
        assert await root.force() == 43


async def test_repl_session_persists_bindings(tmp_path: Path):
    """ReplSession evaluates expressions and files in one persistent Nix scope."""
    nix_file = tmp_path / "uses-binding.nix"
    nix_file.write_text("x + 1")

    async with (
        Session() as session,
        session.store() as store,
        session.repl(store) as repl,
    ):
        assert await repl.line("x = 41") is None
        assert await (await repl.string("x + 1")).force() == 42
        assert await (await repl.file(str(nix_file))).force() == 42


async def test_eval_attr_names(tmp_path: Path):
    """attr_names() returns keys of an attrset (insertion order)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ z = 1; a = 2; m = 3; }")

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        names = await root.attr_names()
        assert set(names) == {"a", "m", "z"}


async def test_eval_has_attr(tmp_path: Path):
    """has_attr() checks for key existence."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ foo = 1; }")

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert await root.has_attr("foo") is True
        assert await root.has_attr("bar") is False


async def test_eval_force_does_not_consume(tmp_path: Path):
    """force_deep() does NOT release the handle — we can force again."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        r1 = await root.force_deep()
        r2 = await root.force_deep()
        assert r1 == r2 == {"a": 1}


async def test_eval_session_cleanup(tmp_path: Path):
    """Handles are released when the eval session exits."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with Session() as session, session.store() as store:
        async with session.eval(store) as eval:
            root = await eval.file(str(nix_file))
            await root.force()
        # Eval session closed — store is still available
        uri = await store.get_uri(GetUriRequest())
        assert isinstance(uri.uri, str)


async def test_eval_thunk(tmp_path: Path):
    """session.file on a file with a thunk (lazy value)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("let x = 1 + 2; in { inherit x; }")

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        assert root.nix_type == NixType.ATTRS
        x = root.attr("x")
        result = await x.force()
        assert result == 3


async def test_eval_nested_navigation(tmp_path: Path):
    """Deep navigation: a.b.c"""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = { b = { c = 99; }; }; }")

    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.file(str(nix_file))
        a = root.attr("a")
        b = a.attr("b")
        c = b.attr("c")
        assert await c.force() == 99


async def test_eval_call_function():
    """ValueProxy.call passes JSON-compatible Python args to a Nix function."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: x + 1")
        assert await fn.force_as(NixType.FUNCTION) is fn
        result = await fn.call(41)
        assert await result.force() == 42


async def test_eval_call_function_with_value_proxy_arg():
    """ValueProxy.call can pass an existing same-session Nix value by handle."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: x.value + 1")
        arg = await eval.string('{ value = 41; ignored = abort "not forced"; }')
        result = await fn(arg)
        assert await result.force_as(NixType.INT) == 42


async def test_eval_reused_function_can_call_separately_evaluated_value():
    """A function proxy can be reused with other values evaluated in the same session."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: y: x + y")
        left = await eval.string("20 + 1")
        right = await eval.string("20 + 1")

        partial = await fn(left)
        result = await partial(right)

        assert await result.force_as(NixType.INT) == 42


async def test_eval_call_function_with_nested_value_proxy_arg():
    """ValueProxy.call can pass same-session Nix values inside copied containers."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: (builtins.elemAt x.items 0).value + builtins.elemAt x.items 1")
        arg = await eval.string('{ value = 40; ignored = abort "not forced"; }')
        result = await fn({"items": [arg, 2]})
        assert await result.force_as(NixType.INT) == 42


async def test_eval_callable_function_proxy():
    """ValueProxy is directly callable when it contains a Nix function."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string("x: x.name")
        result = await fn({"name": "demo"})
        assert await result.force_as(NixType.STRING) == "demo"


async def test_eval_auto_call_function_with_default_attrset_args():
    """auto_call applies Nix top-level call semantics for all-default attrset lambdas."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        fn = await eval.string('{ pkgs ? {}, name ? "demo" }: { inherit name pkgs; }')
        result = await fn.auto_call()

        assert await result.attr("name").force_as(NixType.STRING) == "demo"
        assert await result.has_attr("pkgs")


async def test_eval_call_non_function_raises():
    """Calling a non-function checks the remote type before issuing call RPC."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        value = await eval.string("42")
        with pytest.raises(WrongNixTypeError, match="expected function"):
            await value(1)


async def test_eval_force_as_and_coerce_helpers():
    """force_as checks remote type; coerce_* applies explicit scalar conversions."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        number = await eval.string("42")
        text_number = await eval.string('"42"')
        text_bad = await eval.string('"forty-two"')
        attrs = await eval.string("{ x = 1; }")

        assert await number.force_as(NixType.INT) == 42
        with pytest.raises(WrongNixTypeError):
            await text_number.force_as(NixType.INT)

        assert await text_number.coerce_int() == 42
        assert await number.coerce_str() == "42"
        with pytest.raises(NixCoercionError):
            await text_bad.coerce_int()
        with pytest.raises(NixCoercionError):
            await attrs.coerce_str()


async def test_force_deep_preserves_nested_functions():
    """force_deep recursively forces data but leaves functions callable."""
    async with (
        Session() as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        root = await eval.string("{ x = 1; f = y: y + 2; nested.g = z: z.name; }")
        result = await root.force_deep()

        assert isinstance(result, dict)
        assert result["x"] == 1
        f = result["f"]
        nested = result["nested"]
        assert isinstance(f, ValueProxy)
        assert isinstance(nested, dict)
        g = nested["g"]
        assert isinstance(g, ValueProxy)

        f_result = await f(40)
        assert await f_result.force_as(NixType.INT) == 42

        g_result = await g({"name": "deep"})
        assert await g_result.force_as(NixType.STRING) == "deep"


async def test_evaluated_derivation_can_build_while_eval_session_is_active():
    """ValueProxy.build builds the evaluated derivation through the eval store handle."""
    async with (
        Session() as session,
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
            """
        )

        outputs = await drv.build()

        assert set(outputs) == {"out"}
        assert outputs["out"].startswith("/nix/store/")
        assert "nanopynix-build-value-test" in outputs["out"]
        assert await AnyioPath(outputs["out"]).read_text() == "hello\n"


async def test_evaluated_derivation_can_build_with_explicit_build_store():
    """ValueProxy.build(store=...) uses that store for building, keeping the eval store as context."""
    async with (
        Session() as session,
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
            """
        )

        outputs = await drv.build(store=build_store)

        assert set(outputs) == {"out"}
        assert outputs["out"].startswith("/nix/store/")
        assert "nanopynix-build-value-explicit-store-test" in outputs["out"]
        assert await AnyioPath(outputs["out"]).read_text() == "explicit\n"


@requires_dynamic_primops
async def test_worker_yaml_primops():
    """Importable worker primops parse and render YAML during eval."""
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML "apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: demo\\n"')
        assert await parsed.force_deep() == {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "demo"},
        }

        rendered = await eval.string(
            'builtins.toYAML { apiVersion = "v1"; kind = "ConfigMap"; metadata.name = "demo"; }'
        )
        text = await rendered.force_as(NixType.STRING)
        assert "apiVersion: v1" in text
        assert "kind: ConfigMap" in text
        assert "name: demo" in text


@requires_dynamic_primops
async def test_worker_yaml_primops_parse_yaml12_modes():
    """fromYAML uses modern YAML mode syntax instead of YAML 1.1 octal literals."""
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML "mode1: 0444\\nmode2: 0o444\\nmode3: \\"0444\\"\\n"')
        assert await parsed.force_deep() == {
            "mode1": 444,
            "mode2": 292,
            "mode3": "0444",
        }


@requires_dynamic_primops
async def test_worker_yaml_primops_parse_yaml11_modes():
    """fromYAML11 keeps legacy YAML 1.1 octal parsing for old manifests."""
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML11 "mode: 0444\\ntruth: yes\\n"')
        assert await parsed.force_deep() == {"mode": 292, "truth": True}


@requires_dynamic_primops
async def test_worker_from_yaml_root_list_is_single_document():
    """A root list is still one YAML document, not a document stream."""
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string('builtins.fromYAML "- a\\n- b\\n"')
        assert await parsed.force_deep() == ["a", "b"]


@requires_dynamic_primops
async def test_worker_from_yaml_rejects_document_stream():
    """fromYAML requires exactly one document; streams use fromYAMLStream."""
    async with (
        Session(primops=yaml_primops()) as session,
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
async def test_worker_from_yaml_parse_error_is_descriptive():
    """YAML parse failures include builtin and source location context."""
    async with (
        Session(primops=yaml_primops()) as session,
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
async def test_worker_yaml_stream_primops():
    """YAML stream helpers handle Kubernetes multi-document manifests."""
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        parsed = await eval.string(
            'builtins.fromYAMLStream "apiVersion: v1\\nkind: ConfigMap\\n---\\napiVersion: v1\\nkind: Service\\n"'
        )
        assert await parsed.force_deep() == [
            {"apiVersion": "v1", "kind": "ConfigMap"},
            {"apiVersion": "v1", "kind": "Service"},
        ]

        rendered = await eval.string(
            'builtins.toYAML [ { apiVersion = "v1"; kind = "ConfigMap"; } { apiVersion = "v1"; kind = "Service"; } ]'
        )
        text = await rendered.force_as(NixType.STRING)
        assert text.count("---") == 2
        assert "kind: ConfigMap" in text
        assert "kind: Service" in text


@requires_dynamic_primops
async def test_worker_to_yaml_rejects_functions():
    """toYAML is JSON-compatible data only; nested functions must not stringify."""
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await eval.string("builtins.toYAML { f = x: x; }")
        message = strip_ansi(str(exc_info.value))
        assert "toYAML: argument contains non JSON-compatible Nix value of type" in message
        assert "function" in message
        assert "Python primop" not in message


async def test_eval_concurrent_sessions(tmp_path: Path):
    """Two concurrent eval sessions — each in its own Session."""
    f1 = tmp_path / "a.nix"
    f2 = tmp_path / "b.nix"
    f1.write_text("{ val = 10; }")
    f2.write_text("{ val = 20; }")

    async def eval_one(path: str) -> Any:
        async with (
            Session() as nix,
            nix.store() as store,
            nix.eval(store) as session,
        ):
            root = await session.file(path)
            v = root.attr("val")
            return await v.force()

    results = await asyncio.gather(eval_one(str(f1)), eval_one(str(f2)))
    assert results == [10, 20]


# ── Flake evaluation over RPC ──────────────────────────────────────────


def _init_git_flake(tmp_path: Path, outputs_body: str):
    """Create a temp flake with a git repo for RPC testing."""
    init_flake_repo(tmp_path, outputs_body)


async def test_eval_flake(tmp_path: Path):
    """eval_flake locks and evaluates a flake, returns navigable outputs."""
    _init_git_flake(tmp_path, 'greeting = "hello"; count = 42;')

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        outputs = await eval.eval_flake(str(tmp_path), write_lock_file=False)
        assert outputs.nix_type == NixType.ATTRS
        greeting = outputs.attr("greeting")
        assert await greeting.force() == "hello"
        count = outputs.attr("count")
        assert await count.force() == 42


async def test_eval_flake_force_json(tmp_path: Path):
    """eval_flake + force_json on a sub-attrset serializes it to JSON."""
    _init_git_flake(tmp_path, 'lib = { name = "test"; nested = { x = 1; y = [ "a" "b" ]; }; };')

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        outputs = await eval.eval_flake(str(tmp_path), write_lock_file=False)
        lib = outputs.attr("lib")
        result = await lib.force_json()
        assert isinstance(result, dict)
        assert result["name"] == "test"
        nested = result["nested"]
        assert isinstance(nested, dict)
        assert nested["x"] == 1
        assert nested["y"] == ["a", "b"]


async def test_lock_flake_and_eval_locked(tmp_path: Path):
    """lock_flake + eval_locked_flake: in-memory lock, evaluate without writing."""
    _init_git_flake(tmp_path, "val = 99;")

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()
        assert locked.handle > 0

        outputs = await locked.eval()
        val = outputs.attr("val")
        assert await val.force() == 99


async def test_locked_flake_release_invalidates_handle(tmp_path: Path):
    """release_locked_flake marks the local handle unusable."""
    _init_git_flake(tmp_path, "val = 99;")

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        await locked.release()
        await locked.release()

        with pytest.raises(ValueReleasedError, match="LockedFlakeHandle"):
            await locked.eval()


async def test_lock_flake_write_lock_file(tmp_path: Path):
    """lock_flake with write_lock_file=False, then write_lock_file() persists."""
    _init_git_flake(tmp_path, "val = 1;")

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()

        await locked.write_lock_file()
        assert (tmp_path / "flake.lock").exists()


async def test_lock_flake_no_write_does_not_leak(tmp_path: Path):
    """lock_flake with write_lock_file=False must NOT create flake.lock."""
    _init_git_flake(tmp_path, "val = 1;")

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        await eval.lock_flake(str(tmp_path), write_lock_file=False)
        assert not (tmp_path / "flake.lock").exists()


async def test_lock_flake_update_all(tmp_path: Path):
    """lock_flake with update_inputs=True re-resolves all inputs."""
    _init_git_flake(tmp_path, "x = 1;")

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(
            str(tmp_path),
            update_inputs=True,
            write_lock_file=False,
        )
        assert locked.handle > 0


async def test_lock_flake_update_specific_input(tmp_path: Path):
    """lock_flake with update_inputs=list re-resolves only specified inputs."""
    _init_git_flake(tmp_path, "x = 1;")

    async with (
        Session(experimental_features=["flakes"]) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        locked = await eval.lock_flake(
            str(tmp_path),
            update_inputs=["nonexistent"],
            write_lock_file=False,
        )
        assert locked.handle > 0
