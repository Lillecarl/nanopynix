"""TEMPORARY: does the single exception translator dispatch correctly?

Scaffolding for CIP3 item 3, written alongside the change that replaced
nanobind's one-translator-per-type registration with a single translator owning
the whole ``nix::Error`` hierarchy (``nanopynix-bindings/src/nix_errors.cpp``).

The old arrangement decided which translator won by *registration order*, which
is the order the extension modules happen to be imported in -- unobservable
from any one translation unit. That is why plain ``nix::Error`` was left
unregistered (registering the base of every bound type risked shadowing all of
them), and why anything Nix threw as a plain ``nix::Error`` reached Python as a
bare ``RuntimeError`` with its ``ErrorInfo`` discarded.

So there are three properties worth pinning, and they pull against each other
-- which is exactly why a table of "this expression raises that class" is not
enough on its own:

1. The **catch-all fires**: a plain ``nix::Error`` from Nix's own internals is
   a ``NixError`` carrying ErrorInfo.
2. The **catch-all does not shadow**: every specific subclass still wins, even
   though the catch-all can also match it. A regression here is silent -- you
   get a working exception of the wrong, coarser class.
3. The **catch-all does not overreach**: standard C++ exceptions are *not*
   ours, and must keep falling through to nanobind's default translator.
   Nothing about (1) or (2) would notice if we started swallowing them.

Promote to ``tests/nanopynix`` if it earns its keep; it overlaps the same
boundary A that ``test_error_matrix.py`` records, and the two should probably
land together (see TODO.md item 8 / task #78).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import errors as nanopynix_errors

import nanopynix
from nanopynix import NixEvalSettings

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment


# ════════════════════════════════════════════════════════════════════
# 1. The catch-all fires
# ════════════════════════════════════════════════════════════════════


async def test_a_plain_nix_error_from_nix_itself_is_a_nix_error(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The case that was unreachable while ``nix::Error`` went unregistered.

    ``edit_location()`` on a non-package value reaches
    ``nix::findPackageFilename``, which throws a plain ``nix::Error`` -- not
    any bound subclass. Nothing nanopynix does can change what Nix throws
    there, so before the single translator this arrived as a bare
    ``RuntimeError`` and Nix's position/trace were gone. It is deliberately
    driven through a Nix-internal throw rather than one of our own, because
    our own we could always have fixed by picking a different type.
    """
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as ev,
    ):
        value = await ev.string("42")
        with pytest.raises(nanopynix.NixError) as excinfo:
            await value.edit_location()

    # Not a subclass: Nix threw the base, so the base is the honest answer.
    assert type(excinfo.value) is nanopynix.NixError
    assert excinfo.value.info is not None, "ErrorInfo is what C++ alone has; losing it is unrecoverable"


# ════════════════════════════════════════════════════════════════════
# 2. The catch-all does not shadow the specific subclasses
# ════════════════════════════════════════════════════════════════════

# (expression, expected public class). Each entry is a distinct arm of the
# translator's catch chain, so a mis-ordered chain shows up as a concrete
# failure here rather than as a subtly coarser exception in production.
#
# NixError itself is deliberately absent: "it raised something in the
# hierarchy" is what the catch-all already guarantees, and asserting it here
# would pass even if every arm collapsed into the catch-all.
DISPATCH: list[tuple[str, type[nanopynix.NixError]]] = [
    ('"hi"', nanopynix.NixTypeError),
    ('builtins.throw "boom"', nanopynix.ThrownError),
    ("nope", nanopynix.UndefinedVarError),
    ("assert false; 1", nanopynix.NixAssertionError),
    ("let in in", nanopynix.ParseError),
    ("let x = x + 1; in x", nanopynix.InfiniteRecursionError),
]


async def _force_and_read(ev: Any, expr: str) -> int:
    """Evaluate *expr* and read it as an int, as one step.

    One helper rather than two statements inside ``pytest.raises`` because the
    cases in ``DISPATCH`` do not all fail at the same point: a parse error
    never yields a value at all, ``ev.string`` already forces to WHNF, and a
    type error only surfaces on the read. Which of the two raises is not the
    property under test -- the exception's class is.
    """
    return await (await ev.string(expr)).as_int()


@pytest.mark.parametrize(("expr", "expected"), DISPATCH, ids=[expr for expr, _ in DISPATCH])
async def test_specific_types_still_win_over_the_catch_all(
    expr: str,
    expected: type[nanopynix.NixError],
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as ev,
    ):
        with pytest.raises(nanopynix.NixError) as excinfo:
            await _force_and_read(ev, expr)

    raised = excinfo.value
    assert type(raised) is expected, f"{expr!r} raised {type(raised).__name__}, not {expected.__name__}"
    assert raised.info is not None


async def test_eval_base_error_is_bound_even_though_it_is_not_an_eval_error(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """``StackOverflowError`` hangs off ``EvalBaseError``, bypassing ``EvalError``.

    Its two siblings there, ``IFDError`` and ``RecoverableEvalError``, ride on
    the same arm of the chain but have no expression that reliably provokes
    them from here.

    Driven at an explicit low ``max_call_depth`` rather than Nix's default of
    10000, because the default does not survive the C stack -- see TODO.md
    item 0 / task #80, which is a crash, not an error-mapping problem.
    """
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store, eval_settings=NixEvalSettings(max_call_depth=1000)) as ev,
    ):
        with pytest.raises(nanopynix.EvalError, match="max-call-depth") as excinfo:
            await ev.string("let f = n: f (n + 1); in f 0")

    assert excinfo.value.info is not None
    assert excinfo.value.info["pos"] is not None


def test_the_bound_classes_mirror_nix_own_hierarchy() -> None:
    """Catch-chain order is only half of it; the Python MRO must agree too.

    A caller writing ``except nanopynix_bindings.errors.EvalError`` should
    catch a ``TypeError``, exactly as a C++ ``catch (nix::EvalError &)`` would.
    """
    assert issubclass(nanopynix_errors.TypeError, nanopynix_errors.EvalError)
    assert issubclass(nanopynix_errors.EvalError, nanopynix_errors.EvalBaseError)
    assert issubclass(nanopynix_errors.EvalBaseError, nanopynix_errors.Error)
    assert issubclass(nanopynix_errors.ThrownError, nanopynix_errors.AssertionError)
    # ParseError is a direct child of Error in Nix, NOT an eval error.
    assert issubclass(nanopynix_errors.ParseError, nanopynix_errors.Error)
    assert not issubclass(nanopynix_errors.ParseError, nanopynix_errors.EvalBaseError)


# ════════════════════════════════════════════════════════════════════
# 3. The catch-all does not overreach
# ════════════════════════════════════════════════════════════════════


async def test_standard_cxx_exceptions_are_left_to_nanobind(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """Not everything thrown under a Nix call is a ``nix::Error``.

    Nix throws plain standard exceptions too -- ``std::bad_alloc``,
    nlohmann-json errors, ``std::filesystem::filesystem_error`` -- and so do
    the bindings, for their own precondition checks. The translator must have
    no clause for any of them, so they propagate out of it and reach nanobind's
    ``default_exception_translator``, which maps the standard family well
    (``out_of_range`` -> ``IndexError``, ``bad_alloc`` -> ``MemoryError``, ...).

    This is the property a ``catch (...)`` in the translator would silently
    destroy, turning every one of them into a generic ``NixError``.

    What this test can honestly reach is the *consequence*: a failure that is
    not a Nix error stays out of the Nix hierarchy. ``list_get(99)`` used to be
    the vehicle -- it was our own ``std::out_of_range`` -- but it is now
    deliberately a ``ListIndexError``, an ``IndexError`` *and* a ``NixError``,
    so it no longer demonstrates anything about fall-through.

    Using a released value is the substitute, and it is worth being precise
    about what it does and does not prove. ``nix_expr.cpp`` does throw
    ``std::runtime_error("Nix value has been released")``, but inproc guards
    the same condition in Python first, so what arrives is
    ``InprocValueReleasedError`` and the C++ throw is never reached. The
    assertion below is therefore about the boundary between "Nix said no" and
    "you used the API wrong", not about the translator's decline path.

    The decline path itself has no live trigger left in the public API: after
    this change essentially everything Nix raises *is* a ``nix::Error``, and
    our own ``std::runtime_error`` preconditions are all shadowed by
    Python-side guards like the one above. That is a good state to be in, but
    it does mean a regression that added ``catch (...)`` to the translator
    would not be caught here -- only by the ``std::bad_alloc`` /
    nlohmann-json paths, which cannot be provoked on demand. Worth revisiting
    if this test is promoted.
    """
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as ev,
    ):
        value = await ev.string("42")
        await value.release()
        with pytest.raises(RuntimeError) as excinfo:
            await value.as_int()

    # NixError subclasses RuntimeError, so `pytest.raises(RuntimeError)` above
    # would happily pass on a regression that swallowed this into the
    # hierarchy. This is the assertion that bites.
    assert not isinstance(excinfo.value, nanopynix.NixError)


def test_interrupted_is_not_folded_into_the_error_hierarchy() -> None:
    """``nix::Interrupted`` must stay outside ``NixError``.

    Nix roots its hierarchy at ``BaseError``, not ``Error``, and hangs
    ``Interrupted`` off ``BaseError`` beside ``Error``. Nix says why in
    ``nix/util/error.hh``: "BaseError should generally not be caught, as it has
    Interrupted as a subclass. Catch Error instead." So the translator's
    catch-all is ``nix::Error``, and ``Interrupted`` is handled separately as
    ``KeyboardInterrupt``.

    Asserted structurally rather than by provoking a real interrupt: setting
    Nix's global interrupt flag in-process poisons every later eval in the
    session, which is the same upstream behaviour that already forces
    ``tests/nanopynix/bindings/test_daemon_protocol.py`` to skip. What this
    pins is the consequence that matters -- ``except Exception`` must not
    swallow a cancellation.
    """
    assert issubclass(KeyboardInterrupt, BaseException)
    assert not issubclass(KeyboardInterrupt, Exception)
    assert not issubclass(KeyboardInterrupt, nanopynix.NixError)
    # And no bound class shadows the name, which would make the mapping
    # ambiguous for anyone reading the module.
    assert not hasattr(nanopynix_errors, "Interrupted")


# ════════════════════════════════════════════════════════════════════
# 4. "Not there" is answered the same way for attrsets and lists
# ════════════════════════════════════════════════════════════════════
#
# The two halves of one question, so they must not diverge: a KeyError for the
# attrset and a NixError for the list would be the worst of both. They are
# Python exceptions *as well as* Nix ones because nothing had to be given up to
# make them so -- which is the test below.


async def test_missing_attribute_is_both_a_key_error_and_a_nix_error(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as ev,
    ):
        value = await ev.string("{ foo = 1; bar = 2; }")
        with pytest.raises(nanopynix.MissingAttributeError) as excinfo:
            await value.attr("fooo")

    raised = excinfo.value
    # Both hierarchies, so neither style of caller is wrong.
    assert isinstance(raised, KeyError)
    assert isinstance(raised, nanopynix.NixError)
    # Machine-readable, rather than parsed out of prose by the caller.
    assert raised.key == "fooo"
    # The reason this is built in C++: the candidate names are the attrset's
    # symbol table, so a bare KeyError raised from Python could not have them.
    assert "foo" in raised.suggestions
    # KeyError.__str__ renders repr(args[0]); left to the MRO, every message
    # here would arrive wrapped in quotes.
    assert not str(raised).startswith('"')


async def test_list_index_is_both_an_index_error_and_a_nix_error(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with (
        shared_nix_environment.inproc_session() as session,
        session.store() as store,
        session.eval(store) as ev,
    ):
        value = await ev.string("[ 1 2 ]")
        with pytest.raises(nanopynix.ListIndexError) as excinfo:
            await value.list_get(99)

    raised = excinfo.value
    assert isinstance(raised, IndexError)
    assert isinstance(raised, nanopynix.NixError)
    assert raised.index == 99
    assert raised.info is not None


def test_the_two_absence_errors_agree_on_shape() -> None:
    """Consistency is the requirement; pin it rather than trusting review.

    Neither may drift into being only-Pythonic or only-Nix while the other
    stays put.
    """
    for cls, builtin in (
        (nanopynix.MissingAttributeError, KeyError),
        (nanopynix.ListIndexError, IndexError),
    ):
        assert issubclass(cls, builtin), f"{cls.__name__} stopped being a {builtin.__name__}"
        assert issubclass(cls, nanopynix.EvalError), f"{cls.__name__} left the Nix hierarchy"
        assert issubclass(cls, LookupError), f"{cls.__name__} should still be catchable as LookupError"


# ════════════════════════════════════════════════════════════════════
# Engine parity
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(("expr", "expected"), DISPATCH, ids=[expr for expr, _ in DISPATCH])
async def test_rpc_dispatches_identically_to_inproc(
    expr: str,
    expected: type[nanopynix.NixError],
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """Boundary B must not re-coarsen what boundary A got right.

    The worker prefixes the C++ type name onto the gRPC status and the client
    maps it back, so a translator change on the C++ side reaches rpc callers
    only if that round-trip preserves the new names -- including ``Error``,
    which had no name to send before the catch-all existed.
    """
    session_factory: Any = shared_nix_environment.rpc_session
    async with session_factory() as session, session.store() as store, session.eval(store) as ev:
        with pytest.raises(nanopynix.NixError) as excinfo:
            await _force_and_read(ev, expr)

    assert type(excinfo.value) is expected


async def test_rpc_round_trips_the_catch_all_type_name(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """The one name that had no wire representation before: plain ``Error``.

    Separate from the parametrised parity test above because it is the only
    genuinely *new* name. The others already crossed boundary B; ``Error`` did
    not exist as a bound type, so the worker had nothing to prefix and the
    client had nothing to look up. This fails if ``"Error"`` is missing from
    ``_NIX_EXCEPTION_TYPES`` -- in which case ``split_type_prefix`` declines
    the prefix, the message keeps a stray ``"Error: "`` on the front, and the
    class silently degrades to whatever the message classifier guesses.
    """
    session_factory: Any = shared_nix_environment.rpc_session
    async with session_factory() as session, session.store() as store, session.eval(store) as ev:
        value = await ev.string("42")
        with pytest.raises(nanopynix.NixError) as excinfo:
            await value.edit_location()

    raised = excinfo.value
    assert type(raised) is nanopynix.NixError
    assert not raised.msg.startswith("Error: "), "type prefix was not stripped -- 'Error' is not a known type name"
