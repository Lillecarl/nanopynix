"""Tests for eval over RPC — EvalSession + ValueProxy."""

import asyncio

import pytest

from nanopynix import Nix

pytestmark = pytest.mark.asyncio


async def test_eval_file_simple(tmp_path):
    """eval_file returns a ValueProxy, force() resolves to Python dict."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; b = \"hello\"; c = true; }")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert root.type_name == "attrs"
            result = await root.force()
            assert result == {"a": 1, "b": "hello", "c": True}


async def test_eval_attr_navigation(tmp_path):
    """Navigate into an attrset via .attr(), then force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ inner = { x = 42; y = \"hi\"; }; }")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            inner = await root.attr("inner")
            assert inner.type_name == "attrs"
            x = await inner.attr("x")
            assert x.type_name == "int"
            assert await x.force() == 42


async def test_eval_list(tmp_path):
    """eval_file a list, navigate by index, force."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("[ 1 2 3 ]")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert root.type_name == "list"
            assert await root.list_length() == 3
            first = await root.list_get(0)
            assert first.type_name == "int"
            assert await first.force() == 1


async def test_eval_string(tmp_path):
    """eval_string evaluates an inline expression."""
    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_string("42 + 1")
            assert root.type_name == "int"
            assert await root.force() == 43


async def test_eval_attr_names(tmp_path):
    """attr_names() returns keys of an attrset (insertion order)."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ z = 1; a = 2; m = 3; }")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            names = await root.attr_names()
            assert set(names) == {"a", "m", "z"}


async def test_eval_has_attr(tmp_path):
    """has_attr() checks for key existence."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ foo = 1; }")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert await root.has_attr("foo") is True
            assert await root.has_attr("bar") is False


async def test_eval_force_does_not_consume(tmp_path):
    """force() does NOT release the handle — we can force again."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            r1 = await root.force()
            r2 = await root.force()
            assert r1 == r2 == {"a": 1}


async def test_eval_session_cleanup(tmp_path):
    """Handles are released when the eval session exits."""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = 1; }")

    async with Nix() as nix:
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

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            assert root.type_name == "attrs"
            x = await root.attr("x")
            result = await x.force()
            assert result == 3


async def test_eval_nested_navigation(tmp_path):
    """Deep navigation: a.b.c"""
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ a = { b = { c = 99; }; }; }")

    async with Nix() as nix:
        async with nix.eval() as session:
            root = await session.eval_file(str(nix_file))
            a = await root.attr("a")
            b = await a.attr("b")
            c = await b.attr("c")
            assert await c.force() == 99


async def test_eval_concurrent_sessions(tmp_path):
    """Two concurrent eval sessions — each in its own Session."""
    f1 = tmp_path / "a.nix"
    f2 = tmp_path / "b.nix"
    with open(f1, "w") as f:
        f.write("{ val = 10; }")
    with open(f2, "w") as f:
        f.write("{ val = 20; }")

    async def eval_one(path):
        async with Nix() as nix:
            async with nix.eval() as session:
                root = await session.eval_file(path)
                v = await root.attr("val")
                return await v.force()

    results = await asyncio.gather(eval_one(str(f1)), eval_one(str(f2)))
    assert results == [10, 20]
