from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import util as nanopynix_util

from nanopynix import LogLevel, normalize_log_level
from nanopynix.rpc import Session

if TYPE_CHECKING:
    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, LogLevel.ERROR),
        ("0", LogLevel.ERROR),
        ("ERROR", LogLevel.ERROR),
        ("error", LogLevel.ERROR),
        ("WARN", LogLevel.WARN),
        ("warn", LogLevel.WARN),
        ("NOTICE", LogLevel.NOTICE),
        ("notice", LogLevel.NOTICE),
        ("INFO", LogLevel.INFO),
        ("info", LogLevel.INFO),
        ("TALKATIVE", LogLevel.TALKATIVE),
        ("talkative", LogLevel.TALKATIVE),
        ("CHATTY", LogLevel.CHATTY),
        ("chatty", LogLevel.CHATTY),
        ("DEBUG", LogLevel.DEBUG),
        ("debug", LogLevel.DEBUG),
        ("VOMIT", LogLevel.VOMIT),
        ("vomit", LogLevel.VOMIT),
        ("lvlVomit", LogLevel.VOMIT),
    ],
)
def test_normalize_log_level_accepts_nix_names(raw: int | str, expected: LogLevel) -> None:
    assert normalize_log_level(raw) == expected


@pytest.mark.parametrize("raw", [-1, 8, "loud", ""])
def test_normalize_log_level_rejects_invalid_values(raw: int | str) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_log_level(raw)


async def test_session_updates_live_worker_verbosity() -> None:
    async with Session() as session:
        # INFO, not NOTICE: the worker used to force NOTICE when the caller
        # named no verbosity, while inproc left Nix's compiled-in lvlInfo. The
        # forcing is gone, so both engines now start where Nix does.
        assert await session.get_verbosity() == LogLevel.INFO
        assert await session.set_verbosity("debug") == LogLevel.DEBUG
        assert await session.get_verbosity() == LogLevel.DEBUG


async def test_both_engines_start_at_nixs_own_default_verbosity(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """An unconfigured session logs the same amount whichever engine runs it.

    Measured before the fix: ``get_verbosity()`` answered 3 (INFO) on inproc
    and 2 (NOTICE) on rpc for otherwise identical sessions, because
    ``_worker._init_nix`` substituted NOTICE for an unset verbosity. Nothing
    about running in a subprocess required that, and Nix's own global
    initialises to ``lvlInfo``, so both now leave it alone.
    """
    async with inproc_session() as inproc_nix, rpc_session() as rpc_nix:
        assert await inproc_nix.get_verbosity() == LogLevel.INFO
        assert await rpc_nix.get_verbosity() == LogLevel.INFO


async def _both_doors_agree(factory: Any) -> None:
    """Body of the two-doors test, run once per engine.

    ``Any`` because the two factories are unrelated concrete types and the
    point of the test is that one body drives either -- the same reason
    ``test_store_engine_parity_semantics._run`` is written this way.
    """
    async with factory() as session, session.store() as store, session.eval(store) as evaluator:
        original = await session.get_verbosity()
        try:
            assert await evaluator.get_verbosity() == original, "the evaluator sees a different setting"

            # Write through the evaluator, read through the session.
            assert await evaluator.set_verbosity("chatty") == LogLevel.CHATTY
            assert await session.get_verbosity() == LogLevel.CHATTY

            # ...and the other way round.
            assert await session.set_verbosity("warn") == LogLevel.WARN
            assert await evaluator.get_verbosity() == LogLevel.WARN
        finally:
            await session.set_verbosity(original)


async def _accessors_return_a_log_level(factory: Any) -> None:
    """Body of the return-type test, run once per engine."""
    async with factory() as session, session.store() as store, session.eval(store) as evaluator:
        original = await session.get_verbosity()
        try:
            for level in (await session.get_verbosity(), await evaluator.get_verbosity()):
                assert isinstance(level, LogLevel), f"returned {type(level).__name__}"
                assert level.name.lower() == LogLevel(int(level)).name.lower()
        finally:
            await session.set_verbosity(original)


async def test_verbosity_is_one_process_wide_setting_with_two_doors(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """An EvalSession and its Session read and write the same verbosity.

    inproc's EvalSession had no verbosity accessors at all until this test's
    companion change -- ``test_engine_parity``'s LEDGER carried
    "EvalSession.get_verbosity:rpc-only" for it. Verbosity is process-wide, so
    the fix was to reach the existing setting from the evaluator rather than to
    give the evaluator one of its own; asserting that a write through one door
    is visible through the other is what pins that distinction.

    Save-and-restore rather than asserting a starting value: what the default
    *is* belongs to
    ``test_both_engines_start_at_nixs_own_default_verbosity``, and reading the
    level first keeps this from perturbing the in-process logger for whatever
    runs next.
    """
    await _both_doors_agree(inproc_session)
    await _both_doors_agree(rpc_session)


async def test_a_change_of_level_reaches_a_thread_that_already_ran(
    inproc_session: InprocSessionFactory,
) -> None:
    """A session's level reaches every Nix thread of the session, not one.

    The bindings hold the verbosity in a thread-local, because the Nix global
    is not safe to write while other threads read it. So no thread learns a
    new level of its own accord: ``set_verbosity`` reaches the one thread it
    is dispatched to, and every other thread takes the level from the
    dispatch wrapper, once per operation.

    One evaluator, held open across all three legs, is what gives the test
    its teeth. An evaluator owns a dedicated thread, that thread is not the
    Store thread ``set_verbosity`` runs on, and the first leg makes it take a
    level before the change. Measured: with ``_run_with_log_context`` no
    longer applying the level, this fails at the second assertion. The same
    test written with a fresh evaluator per leg passes either way, because a
    new thread takes the new level from the published default and so hides
    the defect.

    inproc only. The rpc worker runs the same wrapper, in
    ``rpc.worker._state.run_request``, but its threads live in another
    process and reading one directly would need a message on the wire that
    nothing outside this test would ever send. What both engines share is
    covered by ``test_verbosity_is_one_process_wide_setting_with_two_doors``.
    """
    async with inproc_session() as session, session.store() as store, session.eval(store) as evaluator:
        original = await session.get_verbosity()

        async def evaluator_thread_level() -> LogLevel:
            return LogLevel(await evaluator.run(nanopynix_util.get_verbosity))

        try:
            await session.set_verbosity("info")
            assert await evaluator_thread_level() == LogLevel.INFO
            for level in (LogLevel.DEBUG, LogLevel.WARN, LogLevel.VOMIT):
                await session.set_verbosity(level)
                assert await evaluator_thread_level() == level, "the change never reached the evaluator's thread"
        finally:
            await session.set_verbosity(original)


async def test_nix_filters_at_a_pinned_ceiling_that_no_call_moves(
    inproc_session: InprocSessionFactory,
) -> None:
    """``nix::verbosity`` is written once, at import, and never again.

    That single write is the whole fix for the data race ThreadSanitizer found
    on it: the global is a plain ``Verbosity``, every ``debug()`` call site
    reads it on its own thread, and ``set_verbosity`` used to write it from
    whichever Nix thread served the call. A write before any Nix thread exists
    has a happens-before edge to every later read, and a second write anywhere
    gives the race back. This test is what fails if somebody adds one.

    inproc only. The rpc worker pins the same global at its own import, but it
    does so in another process, and reaching it would mean a new message on
    the wire that nothing but this test would ever send.
    """
    ceiling = nanopynix_util.get_log_ceiling()
    if os.environ.get("NANOPYNIX_LOG_CEILING"):
        # The caller moved the ceiling at import, which is what that variable
        # is for. The level below is then not the default, and only the part
        # of this test that no call may move still applies.
        pytest.skip("NANOPYNIX_LOG_CEILING overrides the default ceiling")
    assert ceiling == LogLevel.CHATTY, (
        f"the ceiling is {LogLevel(ceiling).name}, and the default is CHATTY. "
        "Raising it makes every `debug()` site format a message that the filter then drops, "
        "which costs a flake evaluation its RPC deadline. Lowering it drops messages the "
        "filter never sees. `nix_util.cpp` carries the measurement."
    )

    async with inproc_session() as session:
        original = await session.get_verbosity()
        try:
            for level in ("error", "debug", "vomit", "notice"):
                await session.set_verbosity(level)
                assert nanopynix_util.get_log_ceiling() == ceiling, f"set_verbosity({level!r}) moved the ceiling"
        finally:
            await session.set_verbosity(original)


async def test_verbosity_accessors_return_a_LogLevel_not_a_bare_int(  # noqa: N802 -- LogLevel is a type name
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """``.name`` must work on whatever comes back, on either engine.

    inproc used to return a plain ``int``, so ``verbosity.name.lower()`` --
    what ``pynix``'s ``:verbosity`` command does with the result -- raised
    ``AttributeError`` there while working on rpc. ``test_engine_parity``
    compares members and parameter lists, not return types, so it could not
    see this one at all.
    """
    await _accessors_return_a_log_level(inproc_session)
    await _accessors_return_a_log_level(rpc_session)
