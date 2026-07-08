"""Tests for eval over RPC — EvalSession + ValueProxy."""

import asyncio

import pytest

from nanopynix import NixType, Session, ValueProxy, yaml_primops

pytestmark = pytest.mark.asyncio


async def test_eval_file_simple(tmp_path):
    """eval_file returns a ValueProxy, force_deep() resolves to Python dict."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ a = 1; b = "hello"; c = true; }')

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert root.type_name == "attrs"
            result = await root.force_deep()
            assert result == {"a": 1, "b": "hello", "c": True}


async def test_eval_attr_navigation(tmp_path):
    """Navigate into an attrset via .attr(), then force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ inner = { x = 42; y = "hi"; }; }')

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            inner = root.attr("inner")
            assert await inner.get_type() == NixType.ATTRS
            x = inner.attr("x")
            assert await x.get_type() == NixType.INT
            assert await x.force_as(NixType.INT) == 42


async def test_eval_list(tmp_path):
    """eval_file a list, navigate by index, force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("[ 1 2 3 ]")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert root.type_name == "list"
            assert await root.list_length() == 3
            first = root.list_get(0)
            assert await first.type() == NixType.INT
            assert await first.force() == 1


async def test_eval_string(tmp_path):
    """eval_string evaluates an inline expression."""
    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_string("42 + 1")
            assert root.type_name == "int"
            assert await root.force() == 43


async def test_eval_attr_names(tmp_path):
    """attr_names() returns keys of an attrset (insertion order)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ z = 1; a = 2; m = 3; }")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            names = await root.attr_names()
            assert set(names) == {"a", "m", "z"}


async def test_eval_has_attr(tmp_path):
    """has_attr() checks for key existence."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ foo = 1; }")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert await root.has_attr("foo") is True
            assert await root.has_attr("bar") is False


async def test_eval_force_does_not_consume(tmp_path):
    """force_deep() does NOT release the handle — we can force again."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            r1 = await root.force_deep()
            r2 = await root.force_deep()
            assert r1 == r2 == {"a": 1}


async def test_eval_session_cleanup(tmp_path):
    """Handles are released when the eval session exits."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            await root.force()
        # Session closed — worker is available for store calls
        async with nix.store() as store:
            uri = await store.get_uri()
            assert isinstance(uri, str)


async def test_eval_thunk(tmp_path):
    """eval_file on a file with a thunk (lazy value)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("let x = 1 + 2; in { inherit x; }")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert root.type_name == "attrs"
            x = root.attr("x")
            result = await x.force()
            assert result == 3


async def test_eval_nested_navigation(tmp_path):
    """Deep navigation: a.b.c"""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = { b = { c = 99; }; }; }")

    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            a = root.attr("a")
            b = a.attr("b")
            c = b.attr("c")
            assert await c.force() == 99


async def test_eval_call_function():
    """ValueProxy.call passes JSON-compatible Python args to a Nix function."""
    async with Session() as nix:
        async with nix.eval() as session:
            fn = await session.eval_string("x: x + 1")
            assert await fn.force_as(NixType.FUNCTION) is fn
            result = await fn.call(41)
            assert await result.force() == 42


async def test_eval_callable_function_proxy():
    """ValueProxy is directly callable when it contains a Nix function."""
    async with Session() as nix:
        async with nix.eval() as session:
            fn = await session.eval_string("x: x.name")
            result = await fn({"name": "demo"})
            assert await result.force_as(NixType.STRING) == "demo"


async def test_eval_call_non_function_raises():
    """Calling a non-function checks the remote type before issuing call RPC."""
    async with Session() as nix:
        async with nix.eval() as session:
            value = await session.eval_string("42")
            with pytest.raises(TypeError, match="expected function"):
                await value(1)


async def test_force_deep_preserves_nested_functions():
    """force_deep recursively forces data but leaves functions callable."""
    async with Session() as nix:
        async with nix.eval() as session:
            root = await session.eval_string("{ x = 1; f = y: y + 2; nested.g = z: z.name; }")
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


async def test_worker_yaml_primops():
    """Importable worker primops parse and render YAML during eval."""
    async with Session(primops=yaml_primops()) as nix:
        async with nix.eval() as session:
            parsed = await session.eval_string(
                'builtins.fromYAML "apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: demo\\n"'
            )
            assert await parsed.force_deep() == {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "demo"},
            }

            rendered = await session.eval_string(
                'builtins.toYAML { apiVersion = "v1"; kind = "ConfigMap"; metadata.name = "demo"; }'
            )
            text = await rendered.force_as(NixType.STRING)
            assert "apiVersion: v1" in text
            assert "kind: ConfigMap" in text
            assert "name: demo" in text


async def test_worker_to_yaml_rejects_functions():
    """toYAML is JSON-compatible data only; nested functions must not stringify."""
    async with Session(primops=yaml_primops()) as nix:
        async with nix.eval() as session:
            with pytest.raises(Exception, match="non JSON-compatible|Python primop"):
                await session.eval_string("builtins.toYAML { f = x: x; }")


async def test_eval_concurrent_sessions(tmp_path):
    """Two concurrent eval sessions — each in its own Session."""
    f1 = tmp_path / "a.nix"
    f2 = tmp_path / "b.nix"
    with open(f1, "w") as f:
        f.write("{ val = 10; }")
    with open(f2, "w") as f:
        f.write("{ val = 20; }")

    async def eval_one(path):
        async with Session() as nix:
            async with nix.eval() as session:
                root = await session.eval_file(path)
                v = root.attr("val")
                return await v.force()

    results = await asyncio.gather(eval_one(str(f1)), eval_one(str(f2)))
    assert results == [10, 20]
