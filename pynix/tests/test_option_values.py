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
    Trees,
    Value,
    _short,
    at_path,
    rendered,
)
from pynix._options import fetch_option_values, fetch_value_renderer
from pynix._search_target import resolve
from pynix._util import eval_session
from pynix.target import EvaluationTarget, evaluate_target

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from nanopynix_testing.nix_environment import NixTestEnvironment

_MODULE = Path(__file__).parent / "test_search" / "system.nix"
_OPTION = "services.example-daemon"

#: How long a test waits for the pump to answer, in seconds. The tree is
#: already evaluated by then, so one force is under a millisecond.
_SETTLE = 0.05


@pytest.fixture
async def tree(shared_nix_environment: NixTestEnvironment) -> AsyncIterator[Trees]:
    """What one evaluation of the fixture module system gives the pane.

    **Building this forces no default.** The fixture declares two options
    whose default cannot evaluate, so a walk that forced them would fail here
    rather than in the one test that asks for one. It forces no value of the
    configuration either: `config` is selected and not read.
    """
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        target = EvaluationTarget(file=str(_MODULE), attr=None, flake=None)
        value = await evaluate_target(target, session, auto_call_file=True)
        where = await resolve(value)
        if where.options is None or where.lib is None:
            raise AssertionError("the fixture holds both an options tree and a lib")
        yield Trees(
            values=await fetch_option_values(session, where.options.value, where.lib.value),
            config=None if where.config is None else where.config.value,
            render=await fetch_value_renderer(session, where.lib.value),
        )


async def test_a_plain_default_and_example_both_evaluate(tree: Trees) -> None:
    found = await rendered(tree.values, f"{_OPTION}.port")
    assert found.default is not None
    assert found.default.error == ""
    assert found.default.text == "8080"
    assert found.example is not None
    assert found.example.text == "9090"


async def test_an_option_without_a_default_draws_none(tree: Trees) -> None:
    """`None` is not the same answer as an empty string, and the pane obeys it."""
    found = await rendered(tree.values, f"{_OPTION}.withoutDefault")
    assert found.default is None
    assert found.example is None


async def test_a_thrown_default_reports_the_message(tree: Trees) -> None:
    """`throw` is the failure `builtins.tryEval` does catch."""
    found = await rendered(tree.values, f"{_OPTION}.thrownDefault")
    assert found.default is not None
    assert "this default is not available here" in found.default.error
    assert found.default.text == ""


async def test_a_missing_attribute_default_reports_the_message(tree: Trees) -> None:
    """The failure that `builtins.tryEval` explicitly cannot catch.

    `pynix._options` leaves `default` out of the bulk walk for this exact
    shape, and its docstring says so. Across the binding boundary it is an
    ordinary exception, which is the whole reason this module exists.
    """
    found = await rendered(tree.values, f"{_OPTION}.brokenDefault")
    assert found.default is not None
    assert "doesNotExist" in found.default.error


async def test_a_described_default_answers_from_its_text(tree: Trees) -> None:
    """`defaultText` comes first, so the default it describes is never forced.

    The fixture makes that observable: the default under the text is a
    `throw`, and this reads the text with no error.
    """
    found = await rendered(tree.values, f"{_OPTION}.describedDefault")
    assert found.default is not None
    assert found.default.error == ""
    assert found.default.text == '"the name of the host"'


async def test_one_bad_default_costs_no_other_option(tree: Trees) -> None:
    """The session stays usable after a failure, and the next option answers.

    This is the property the bulk walk cannot have: one Nix list forced in one
    JSON pass makes one bad default the failure of every option.
    """
    broken = await rendered(tree.values, f"{_OPTION}.brokenDefault")
    thrown = await rendered(tree.values, f"{_OPTION}.thrownDefault")
    good = await rendered(tree.values, f"{_OPTION}.port")
    assert broken.default is not None
    assert broken.default.error != ""
    assert thrown.default is not None
    assert thrown.default.error != ""
    assert good.default is not None
    assert good.default.text == "8080"


async def test_a_sub_option_of_a_submodule_answers(tree: Trees) -> None:
    """The keys are what the metadata walk writes, placeholder and all."""
    found = await rendered(tree.values, f"{_OPTION}.vhosts.<name>.port")
    assert found.default is not None
    assert found.default.text == "80"


# -- the pump that serves the detail pane ----------------------------------


def _counting(tree: Trees, opened: list[int]) -> Callable[[], AbstractAsyncContextManager[Trees]]:
    """An opener over the real trees, which counts how many times it ran."""

    @contextlib.asynccontextmanager
    async def open_trees() -> AsyncGenerator[Trees]:
        opened.append(1)
        yield tree

    return open_trees


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


async def test_one_evaluator_serves_every_option(tree: Trees) -> None:
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


async def test_a_search_that_asks_for_nothing_opens_no_evaluator(tree: Trees) -> None:
    """The measurement that `_values` promises.

    A reader who types a query and reads the names never reaches the
    evaluator, so a warm search stays as fast as the cache makes it.
    """
    opened: list[int] = []
    values = OptionValues(_counting(tree, opened))
    await _served(values, [])
    assert opened == []


async def test_only_the_newest_request_is_served(tree: Trees) -> None:
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


async def test_the_pane_is_redrawn_when_an_answer_arrives(tree: Trees) -> None:
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
    async def broken() -> AsyncGenerator[Trees]:
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


# -- what the option came to, at the path the reader typed -------------------
#
# `options` declares an option and `config` says what it came to. A record
# stands for every instance of an `attrsOf (submodule ...)` option, so only
# the query says which value to read: `services.example-daemon.vhosts.<name>.port`
# is one record, and `services.example-daemon.vhosts.web.port` is a value.

_CONFIGURED = Path(__file__).parent / "test_search" / "configured.nix"


@pytest.fixture
async def configured(shared_nix_environment: NixTestEnvironment) -> AsyncIterator[Trees]:
    """The fixture module system with values set, and nothing forced."""
    async with eval_session(shared_nix_environment.store_uri) as (_nix, _store, session):
        target = EvaluationTarget(file=str(_CONFIGURED), attr=None, flake=None)
        value = await evaluate_target(target, session, auto_call_file=True)
        where = await resolve(value)
        if where.options is None or where.lib is None or where.config is None:
            raise AssertionError("the fixture holds an options tree, a lib and a config")
        yield Trees(
            values=await fetch_option_values(session, where.options.value, where.lib.value),
            config=where.config.value,
            render=await fetch_value_renderer(session, where.lib.value),
        )


async def test_a_path_reads_what_the_configuration_set(configured: Trees) -> None:
    """The module sets the port to 9999, and the default is still 8080."""
    found = await at_path(configured, ("services", "example-daemon", "port"))
    assert found is not None
    assert found.error == ""
    assert found.text == "9999"


async def test_an_instance_of_a_placeholder_reads_its_own_value(configured: Trees) -> None:
    """`vhosts.<name>.port` is one record, and `vhosts.web.port` is a value."""
    found = await at_path(configured, ("services", "example-daemon", "vhosts", "web", "port"))
    assert found is not None
    assert found.text == "8081"


async def test_a_path_that_is_not_there_answers_nothing(configured: Trees) -> None:
    """A reader part-way through typing names a path that does not exist yet.

    That is not an error, and a line saying so would be on the screen for most
    of the time a reader spends typing.
    """
    assert await at_path(configured, ("services", "example-daemon", "vhosts", "absent", "port")) is None
    assert await at_path(configured, ("nothing", "here")) is None


async def test_a_target_with_no_config_reads_no_value(tree: Trees) -> None:
    """`system.nix` holds a `config`, and a bare options attrset does not.

    The pane then draws the declaration alone, which is the truth about that
    target rather than a failure of it.
    """
    without = Trees(values=tree.values, config=None, render=tree.render)
    assert await at_path(without, ("services", "example-daemon", "port")) is None


async def test_an_empty_path_reads_no_value(configured: Trees) -> None:
    """A query that binds no path asks for nothing at all."""
    assert await at_path(configured, ()) is None


async def test_the_resolver_answers_a_declaration_and_a_value_together(configured: Trees) -> None:
    """What the pane calls: one option, one concrete path, one answer."""
    values = OptionValues(_counting(configured, []))
    segments = ("services", "example-daemon", "vhosts", "web", "port")
    await _served(values, [lambda: values.known(f"{_OPTION}.vhosts.<name>.port", segments)])
    answer = values.known(f"{_OPTION}.vhosts.<name>.port", segments)
    assert answer is not None
    assert answer.default is not None
    assert answer.default.text == "80"
    assert answer.value is not None
    assert answer.value.text == "8081"


async def test_a_declaration_stays_answered_while_a_new_path_resolves(configured: Trees) -> None:
    """One more character changes the path and not the option.

    Recomputing both would put the pending line back on the screen for a field
    that had not changed, on every keystroke.
    """
    values = OptionValues(_counting(configured, []))
    name = f"{_OPTION}.vhosts.<name>.port"
    await _served(values, [lambda: values.known(name, ("services", "example-daemon", "vhosts", "web", "port"))])
    # A path nothing has resolved yet, and the same option.
    answer = values.known(name, ("services", "example-daemon", "vhosts", "other", "port"))
    assert answer is not None
    assert answer.default is not None
    assert answer.default.text == "80"
    assert answer.value is None
