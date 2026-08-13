"""The evaluation statistics of an evaluator, on both engines.

`NIX_SHOW_STATS=1 nix` prints a report of an evaluation. `statistics` returns
the same report, and `nix-2.34-count-calls.patch` is what makes it reachable
from an embedded evaluator.

**Two switches control the report, and each one covers a different half.**
Nothing in the names says so, and each switch fails quietly on its own:

* `count_calls`, an eval setting, fills the `primops`, `functions` and
  `attributes` tables. With it off, the three keys are absent.
* `set_eval_counters_enabled`, which names a process, fills every numeric
  field. With it off, `nix::Counter::enabled` is false and each increment is
  a no-op, so `values` and `nrFunctionCalls` report zero while the three
  tables above still fill correctly.

The second one is the trap. A test that asserts on the tables alone passes
with every number zero, which is how the defect reached a commit during the
work that added this file.

**The numeric fields are not testable on inproc, and this file does not test
them there.** The switch and two of the counters are statics of the process,
so every evaluator of that process shares them. inproc evaluates in parallel
in one process, so the numbers depend on what the other evaluators did and
when they did it. rpc gives each worker its own process, which is the only
reason its numbers are stable.

That is a property of where Nix keeps the counters, and not a defect of the
report. #118 moves the switch and the counters onto the evaluator, and the
skips below name it. Delete each skip when that issue lands, because the
numbers become the evaluator's own and the tests then mean something.

The tables need no such care. `count_calls` writes them into the maps of one
evaluator, so they are exact on both engines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

import nanopynix
from nanopynix.settings import NixEvalSettings

if TYPE_CHECKING:
    from collections.abc import Callable


# 50 multiplications, and a fold over the 50 results. Each count below is a
# consequence of this expression, so a wrong count names the field that broke.
EXPR = "builtins.foldl' (a: b: a + b) 0 (builtins.genList (i: i * 2) 50)"
MULTIPLICATIONS = 50

TABLES = ("primops", "functions", "attributes")

# `build_info` returns an untyped dict from the bindings, so the cast is what
# gives pyright a type to work with here.
_BUILD_INFO = cast("dict[str, Any]", nanopynix.build_info())  # pyright: ignore[reportUnknownMemberType] -- build_info is a nanobind function that returns an untyped dict, so the call itself is unknown and the cast alone cannot answer it
_CAPABILITIES = cast("dict[str, Any]", _BUILD_INFO["capabilities"])

pytestmark = pytest.mark.skipif(
    not _CAPABILITIES["eval_statistics"],
    reason="the count-calls patch does not reach this Nix version -- 2.31 has no report",
)


async def _report(
    factory: Callable[[], Any],
    *,
    count_calls: bool,
    counters: bool,
) -> dict[str, Any]:
    """Evaluate ``EXPR`` under the two switches, and return the report."""
    settings = NixEvalSettings(count_calls=True) if count_calls else None
    async with (
        factory() as session,
        session.store() as store,
        session.eval(store, eval_settings=settings) as ev,
    ):
        await ev.set_eval_counters_enabled(counters)
        await (await ev.string(EXPR)).to_python()
        return await ev.statistics()


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "inproc",
            marks=pytest.mark.skip(
                reason="inproc evaluates in parallel in one process, and the counters are "
                "statics of that process, so the numbers are not predictable -- see #118",
            ),
        ),
        "rpc",
    ],
)
async def test_the_counters_switch_fills_the_numeric_fields(
    engine: str,
    inproc_session: Any,
    rpc_session: Any,
) -> None:
    """The switch that a test of the tables alone would not notice.

    With the counters off, every numeric field is zero, because
    `nix::Counter::enabled` is false and each increment returns without
    writing. The report is still a whole document, so nothing raises.
    """
    factory = inproc_session if engine == "inproc" else rpc_session

    off = await _report(factory, count_calls=True, counters=False)
    on = await _report(factory, count_calls=True, counters=True)

    assert off["values"]["number"] == 0
    assert off["nrFunctionCalls"] == 0
    assert on["values"]["number"] > 0
    assert on["nrFunctionCalls"] > 0


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_the_count_calls_setting_fills_the_three_tables(
    engine: str,
    inproc_session: Any,
    rpc_session: Any,
) -> None:
    """`count_calls` is the other switch, and it is independent of the first."""
    factory = inproc_session if engine == "inproc" else rpc_session

    without = await _report(factory, count_calls=False, counters=True)
    with_tables = await _report(factory, count_calls=True, counters=True)

    assert [table for table in TABLES if table in without] == []
    assert [table for table in TABLES if table in with_tables] == list(TABLES)
    # The count is a consequence of EXPR, and not a number that happened to
    # appear: `genList (i: i * 2) 50` multiplies exactly 50 times.
    assert with_tables["primops"]["mul"] == MULTIPLICATIONS


async def test_both_engines_count_the_same_calls(
    inproc_session: Any,
    rpc_session: Any,
) -> None:
    """Process isolation is the only thing rpc has that inproc does not.

    This asserts on the three tables, and not on the numeric fields. The
    tables are exact: `count_calls` writes them into the evaluator's own maps,
    and `EXPR` decides each number. The numeric fields cannot be compared this
    way: inproc evaluates in parallel in one process and the counters are
    statics of it, so the numbers there depend on the other evaluators. The
    tables agree in every run, on both engines.
    """
    inproc = await _report(inproc_session, count_calls=True, counters=True)
    rpc = await _report(rpc_session, count_calls=True, counters=True)

    assert inproc["primops"]["mul"] == rpc["primops"]["mul"] == MULTIPLICATIONS
    assert inproc["primops"] == rpc["primops"]
    assert len(inproc["functions"]) == len(rpc["functions"])


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
async def test_the_report_holds_the_fields_that_nix_prints(
    engine: str,
    inproc_session: Any,
    rpc_session: Any,
) -> None:
    """The shape of the document, and not the value of each field.

    Nix decides the fields and changes them between versions, so this asserts
    on the ones that every supported version supplies.
    """
    factory = inproc_session if engine == "inproc" else rpc_session

    report = await _report(factory, count_calls=False, counters=True)

    for key in ("cpuTime", "envs", "list", "nrExprs", "sets", "symbols", "values"):
        assert key in report, f"{key} is absent from the report"
    assert isinstance(report["values"]["number"], int)


@pytest.mark.skip(
    reason="the assertion reads a numeric field, which inproc cannot make predictable "
    "while the counters are statics of a process that evaluates in parallel -- see #118",
)
async def test_two_evaluators_share_the_process_switch(
    inproc_session: Any,
) -> None:
    """The half that stays wrong, recorded so that #118 finds it.

    The switch is one static of the process, so an evaluator cannot count
    while another one beside it does not.

    This test reads `values.number`, and inproc cannot make that number
    predictable today: it evaluates in parallel in one process, and every
    evaluator of that process shares the counters. So the test is skipped
    rather than deleted, because the shape of the check is the one to keep.
    When #118 lands, delete the skip and assert the opposite: `quiet` counts
    nothing while `counting` counts.
    """
    async with (
        inproc_session() as session,
        session.store() as store,
        session.eval(store) as counting,
        session.eval(store) as quiet,
    ):
        await counting.set_eval_counters_enabled(True)
        await (await quiet.string(EXPR)).to_python()

        report = await quiet.statistics()

    # `quiet` never asked to count, and it counted anyway, because `counting`
    # turned the one static of the process on.
    assert report["values"]["number"] > 0
