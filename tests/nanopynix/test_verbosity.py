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


async def _an_evaluator_owns_its_level(factory: Any) -> None:
    """Body of the per-evaluator contract test, run once per engine.

    ``Any`` because the two factories are unrelated concrete types and the
    point of the test is that one body drives either -- the same reason
    ``test_store_engine_parity_semantics._run`` is written this way.
    """
    async with factory() as session, session.store() as store, session.eval(store) as evaluator:
        original = await session.get_verbosity()
        try:
            # It follows the session until it sets a level of its own.
            assert await evaluator.get_verbosity() == original, "a new evaluator does not follow its session"
            assert await session.set_verbosity("warn") == LogLevel.WARN
            assert await evaluator.get_verbosity() == LogLevel.WARN, "a follower did not move with its session"

            # A write through the evaluator stays there.
            assert await evaluator.set_verbosity("chatty") == LogLevel.CHATTY
            assert await evaluator.get_verbosity() == LogLevel.CHATTY
            assert await session.get_verbosity() == LogLevel.WARN, "the evaluator moved its session"

            # And it is sticky: the session no longer moves it.
            assert await session.set_verbosity("error") == LogLevel.ERROR
            assert await evaluator.get_verbosity() == LogLevel.CHATTY, "a session write moved a pinned evaluator"
        finally:
            await session.set_verbosity(original)


async def _two_evaluators_hold_two_levels(factory: Any) -> None:
    """Body of the two-evaluators test, run once per engine.

    Neither reads nor writes the session's level. The relation between an
    evaluator and its session is ``_an_evaluator_owns_its_level``'s subject,
    and leaving it out here keeps this body to one claim.
    """
    async with (
        factory() as session,
        session.store() as store,
        session.eval(store) as quiet,
        session.eval(store) as loud,
    ):
        assert await quiet.set_verbosity("error") == LogLevel.ERROR
        assert await loud.set_verbosity("vomit") == LogLevel.VOMIT

        # Both open, both read back, neither moved the other.
        assert await quiet.get_verbosity() == LogLevel.ERROR
        assert await loud.get_verbosity() == LogLevel.VOMIT


async def _a_store_keeps_the_sessions_level(factory: Any) -> None:
    """Body of the Store-scope test, run once per engine."""
    async with factory() as session, session.store() as store:
        original = await session.get_verbosity()
        try:
            await session.set_verbosity("warn")
            async with session.eval(store) as evaluator:
                await evaluator.set_verbosity("vomit")
                # A store operation, dispatched through the store rather than
                # through the evaluator. `is_valid_path` because every engine
                # and every backend answers it without writing anything.
                await store.is_valid_path("/nix/store/00000000000000000000000000000000-nothing")
                assert await session.get_verbosity() == LogLevel.WARN, "an evaluator moved its session's store work"
                assert await evaluator.get_verbosity() == LogLevel.VOMIT
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


async def test_an_evaluator_owns_its_verbosity_and_falls_back_to_its_session(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """An evaluator's level is its own, and it follows its session until it is.

    This test used to hold the opposite contract, under the name
    ``test_verbosity_is_one_process_wide_setting_with_two_doors``: a write
    through either door had to be visible through the other. That was correct
    while ``nix::verbosity`` was one global that every thread read. The pin of
    that global moved the filter into a thread-local, and the level now
    belongs to whatever the caller dispatched through.

    Three legs, and each one fails on its own defect. The follower leg fails
    if the evaluator snapshots the session's level rather than reading it
    live. The isolation leg fails if ``EvalSession.set_verbosity`` still
    writes the session's level, which is also what would move the
    process-wide default under every other evaluator. The sticky leg fails if
    the evaluator keeps reading the session after it has a level of its own.

    Save-and-restore rather than asserting a starting value: what the default
    *is* belongs to
    ``test_both_engines_start_at_nixs_own_default_verbosity``, and reading the
    level first keeps this from perturbing the in-process logger for whatever
    runs next.
    """
    await _an_evaluator_owns_its_level(inproc_session)
    await _an_evaluator_owns_its_level(rpc_session)


async def test_two_evaluators_of_one_session_hold_different_levels(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """Two open evaluators, two levels, at the same time.

    Both evaluators are open across the whole block, which is what separates
    this from the test above. inproc gives each evaluator a dedicated Nix
    thread, and the rpc worker does the same, so a level that leaked between
    them would be one thread overwriting the other's. Opening one evaluator at
    a time would pass either way.
    """
    await _two_evaluators_hold_two_levels(inproc_session)
    await _two_evaluators_hold_two_levels(rpc_session)


async def test_a_store_logs_at_its_sessions_level_not_an_evaluators(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """A store is session-scoped, and an evaluator's level does not reach it.

    A store dispatches through its session -- ``Session.run`` on inproc, a
    request with no evaluator handle on rpc -- and both engines run store work
    on a shared thread pool. So the pool thread takes the session's level at
    the start of every request, and an evaluator of that session sitting at
    ``VOMIT`` changes nothing about it.

    On inproc the last assertion is the direct probe: ``Session.get_verbosity``
    dispatches through the same store pool and reports what that thread
    carries, so a store call that left the evaluator's level behind is what it
    reads. The store call before it is there to leave something behind. On rpc
    the same read goes to the worker's executor rather than the store limiter,
    so that leg is a contract test and not a probe.
    """
    await _a_store_keeps_the_sessions_level(inproc_session)
    await _a_store_keeps_the_sessions_level(rpc_session)


async def _warnings_each_evaluator_emits(factory: Any) -> tuple[list[str], list[str]]:
    """Evaluate ``builtins.warn`` on two evaluators at two levels.

    Returns what each capture recorded, quiet evaluator first.
    ``builtins.warn`` because it logs at ``lvlWarn``: ``ERROR`` drops it and
    ``INFO`` keeps it, so one expression answers both halves. Every supported
    Nix version has it -- ``prim_warn`` is in ``primops.cc`` of 2.31, 2.34 and
    2.35.

    One capture for each evaluation, rather than one capture around both. A
    capture subscribes to the session's whole bus, so a shared one would
    record both evaluators and the test would then have to attribute each
    event to the operation that produced it.
    """

    def messages(events: Any, marker: str) -> list[str]:
        return [text for event in events if (text := event.message_without_ansi) is not None and marker in text]

    async with (
        factory() as session,
        session.store() as store,
        session.eval(store) as quiet,
        session.eval(store) as loud,
    ):
        await quiet.set_verbosity("error")
        await loud.set_verbosity("info")
        async with session.capture_logs() as quiet_logs:
            await (await quiet.string('builtins.warn "nanopynix quiet marker" 1')).to_python()
        async with session.capture_logs() as loud_logs:
            await (await loud.string('builtins.warn "nanopynix loud marker" 1')).to_python()
        return (
            messages(quiet_logs.events, "nanopynix quiet marker"),
            messages(loud_logs.events, "nanopynix loud marker"),
        )


async def test_each_evaluator_filters_its_own_events_at_its_own_level(
    inproc_session: InprocSessionFactory,
    rpc_session: RpcSessionFactory,
) -> None:
    """An evaluator's level filters its events, and not only its answer.

    ``get_verbosity`` reads back a number, and a number that round-trips
    proves nothing about the filter: an evaluator could report ``ERROR`` while
    its thread still carried the session's level, and only a message that
    should have been dropped would show it. So this drives the filter itself,
    through the one path that reaches it -- Nix logging on the evaluator's own
    Nix thread.

    Both evaluators stay open for both legs, so this also covers the case the
    number test cannot: the quiet evaluator's level surviving an operation on
    the loud one.
    """
    for factory in (inproc_session, rpc_session):
        quiet, loud = await _warnings_each_evaluator_emits(factory)
        assert quiet == [], f"an evaluator at ERROR emitted a warning: {quiet}"
        assert len(loud) == 1, f"an evaluator at INFO dropped its warning, or repeated it: {loud}"


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

    The evaluator here never sets a level of its own, so it follows the
    session throughout. An evaluator that pins one stops following, and
    ``test_an_evaluator_owns_its_verbosity_and_falls_back_to_its_session``
    holds that half.

    inproc only. The rpc worker runs the same wrapper, in
    ``rpc.worker._state.run_request``, but its threads live in another
    process and reading one directly would need a message on the wire that
    nothing outside this test would ever send. What both engines share is
    covered by
    ``test_an_evaluator_owns_its_verbosity_and_falls_back_to_its_session``.
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
