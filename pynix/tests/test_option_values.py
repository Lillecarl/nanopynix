"""Tests for the per-option force of ``default`` and ``example``.

**Every case here runs against a real evaluator.** The point of this module is
that a failure which ``builtins.tryEval`` cannot catch inside Nix arrives in
Python as an ordinary exception, and no double can show that. The fixture at
``test_search/module.nix`` declares one option for each shape: a plain
default, a default that ``throw`` raises, a default that reads an attribute
that is not there, a ``defaultText`` and an option with no default at all.

The pump that serves a request from the detail pane needs no evaluator, so
its tests use a stub tree.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
import pytest

from pynix._option_values import (
    LIMIT,
    EvaluatorUnavailableError,
    OptionValues,
    Rendered,
    Value,
    _short,
    rendered,
)
from pynix._options import fetch_option_values
from pynix._search_target import resolve
from pynix._util import eval_session
from pynix.target import EvaluationTarget, evaluate_target

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from nanopynix import AsyncValue
    from nanopynix_testing.nix_environment import NixTestEnvironment

_MODULE = Path(__file__).parent / "test_search" / "system.nix"
_OPTION = "services.example-daemon"

#: How long a test waits for the pump to answer, in seconds. The tree is
#: already evaluated by then, so one force is under a millisecond.
_SETTLE = 0.05


@pytest.fixture
async def tree(shared_nix_environment: NixTestEnvironment) -> AsyncIterator[AsyncValue]:
    """The lazy attrset of option values, from the fixture module system.

    **Building this forces no default.** The fixture declares two options
    whose default cannot evaluate, so a walk that forced them would fail here
    rather than in the one test that asks for one.
    """
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        target = EvaluationTarget(file=str(_MODULE), attr=None, flake=None)
        value = await evaluate_target(target, session, auto_call_file=True)
        where = await resolve(value)
        if where.options is None or where.lib is None:
            raise AssertionError("the fixture holds both an options tree and a lib")
        yield await fetch_option_values(session, where.options.value, where.lib.value)


async def test_a_plain_default_and_example_both_evaluate(tree: AsyncValue) -> None:
    found = await rendered(tree, f"{_OPTION}.port")
    assert found.default is not None
    assert found.default.error == ""
    assert found.default.text == "8080"
    assert found.example is not None
    assert found.example.text == "9090"


async def test_an_option_without_a_default_draws_none(tree: AsyncValue) -> None:
    """`None` is not the same answer as an empty string, and the pane obeys it."""
    found = await rendered(tree, f"{_OPTION}.withoutDefault")
    assert found.default is None
    assert found.example is None


async def test_a_thrown_default_reports_the_message(tree: AsyncValue) -> None:
    """`throw` is the failure `builtins.tryEval` does catch."""
    found = await rendered(tree, f"{_OPTION}.thrownDefault")
    assert found.default is not None
    assert "this default is not available here" in found.default.error
    assert found.default.text == ""


async def test_a_missing_attribute_default_reports_the_message(tree: AsyncValue) -> None:
    """The failure that `builtins.tryEval` explicitly cannot catch.

    `pynix._options` leaves `default` out of the bulk walk for this exact
    shape, and its docstring says so. Across the binding boundary it is an
    ordinary exception, which is the whole reason this module exists.
    """
    found = await rendered(tree, f"{_OPTION}.brokenDefault")
    assert found.default is not None
    assert "doesNotExist" in found.default.error


async def test_a_described_default_answers_from_its_text(tree: AsyncValue) -> None:
    """`defaultText` comes first, so the default it describes is never forced.

    The fixture makes that observable: the default under the text is a
    `throw`, and this reads the text with no error.
    """
    found = await rendered(tree, f"{_OPTION}.describedDefault")
    assert found.default is not None
    assert found.default.error == ""
    assert found.default.text == '"the name of the host"'


async def test_one_bad_default_costs_no_other_option(tree: AsyncValue) -> None:
    """The session stays usable after a failure, and the next option answers.

    This is the property the bulk walk cannot have: one Nix list forced in one
    JSON pass makes one bad default the failure of every option.
    """
    broken = await rendered(tree, f"{_OPTION}.brokenDefault")
    thrown = await rendered(tree, f"{_OPTION}.thrownDefault")
    good = await rendered(tree, f"{_OPTION}.port")
    assert broken.default is not None
    assert broken.default.error != ""
    assert thrown.default is not None
    assert thrown.default.error != ""
    assert good.default is not None
    assert good.default.text == "8080"


async def test_a_sub_option_of_a_submodule_answers(tree: AsyncValue) -> None:
    """The keys are what the metadata walk writes, placeholder and all."""
    found = await rendered(tree, f"{_OPTION}.vhosts.<name>.port")
    assert found.default is not None
    assert found.default.text == "80"


# -- the pump that serves the detail pane ----------------------------------


def _counting(tree: AsyncValue, opened: list[int]) -> Callable[[], AbstractAsyncContextManager[AsyncValue]]:
    """An opener over the real tree, which counts how many times it ran."""

    @contextlib.asynccontextmanager
    async def open_tree() -> AsyncGenerator[AsyncValue]:
        opened.append(1)
        yield tree

    return open_tree


async def _served(
    values: OptionValues,
    asks: Sequence[Callable[[], object]],
    redraw: Callable[[], None] = lambda: None,
) -> None:
    """Run the pump, place each request in *asks* in turn, and stop the pump.

    Each request gets its own settle, because one render places one request:
    the pane draws the selected option and asks about that one alone.
    """
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(values.serve, redraw)
        await anyio.lowlevel.checkpoint()
        for ask in asks:
            ask()
            await anyio.sleep(_SETTLE)
        await anyio.sleep(_SETTLE)
        tasks.cancel_scope.cancel()


async def test_one_evaluator_serves_every_option(tree: AsyncValue) -> None:
    """The session opens once, and every later request reuses it."""
    opened: list[int] = []
    values = OptionValues(_counting(tree, opened))
    await _served(
        values,
        [lambda: values.known(f"{_OPTION}.port"), lambda: values.known(f"{_OPTION}.extraConfig")],
    )
    assert opened == [1]
    for name, text in ((f"{_OPTION}.port", "8080"), (f"{_OPTION}.extraConfig", '""')):
        answer = values.known(name)
        assert answer is not None
        assert answer.default is not None
        assert answer.default.text == text


async def test_a_search_that_asks_for_nothing_opens_no_evaluator(tree: AsyncValue) -> None:
    """The measurement that `_values` promises.

    A reader who types a query and reads the names never reaches the
    evaluator, so a warm search stays as fast as the cache makes it.
    """
    opened: list[int] = []
    values = OptionValues(_counting(tree, opened))
    await _served(values, [])
    assert opened == []


async def test_only_the_newest_request_is_served(tree: AsyncValue) -> None:
    """A reader who moves through ten options wants the tenth.

    The pane asks about the selected option on every render, so a request that
    the pump drops here comes back on the next render. Nothing is lost, and a
    scroll does not queue ten evaluations.
    """
    values = OptionValues(_counting(tree, []))

    def both() -> None:
        values.known(f"{_OPTION}.port")
        values.known(f"{_OPTION}.extraConfig")

    await _served(values, [both])
    assert values.known(f"{_OPTION}.port") is None
    assert values.known(f"{_OPTION}.extraConfig") is not None


async def test_the_pane_is_redrawn_when_an_answer_arrives(tree: AsyncValue) -> None:
    """`known` answers `None` first, and the redraw is what fills the pane in."""
    drawn: list[int] = []
    values = OptionValues(_counting(tree, []))
    await _served(values, [lambda: values.known(f"{_OPTION}.port")], lambda: drawn.append(1))
    assert drawn == [1]
    assert values.known(f"{_OPTION}.port") == Rendered(
        default=Value(text="8080"),
        example=Value(text="9090"),
    )


async def test_an_evaluator_that_cannot_open_is_asked_once() -> None:
    """A failure to open is the same failure every time, so it is remembered.

    Without this, every keypress paid for a whole evaluation that had already
    failed.
    """
    tries: list[int] = []

    @contextlib.asynccontextmanager
    async def broken() -> AsyncGenerator[AsyncValue]:
        tries.append(1)
        raise EvaluatorUnavailableError("the target holds no options tree")
        yield  # pragma: no cover -- unreachable, and the generator needs it

    values = OptionValues(broken)
    await _served(values, [lambda: values.known("a"), lambda: values.known("b")])
    assert tries == [1]
    answer = values.known("b")
    assert answer is not None
    assert answer.default is not None
    assert "no options tree" in answer.default.error


def test_a_long_value_is_cut() -> None:
    """A default that prints megabytes must not be one fragment of the pane."""
    assert len(_short("x" * (LIMIT * 2))) < LIMIT * 2
    assert _short("x" * 10) == "x" * 10
