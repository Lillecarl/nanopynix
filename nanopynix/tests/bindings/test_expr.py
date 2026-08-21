"""Tests for nanopynix_expr (EvalState, Value, eval_string, eval_file, call)."""

from __future__ import annotations

import gc
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import expr as nanopynix_expr

import nanopynix
from nanopynix_testing.nix_markers import LINUX_PROC_FS
from test_support.notes import note

if TYPE_CHECKING:
    from collections.abc import Callable


# Every test here drives the compiled evaluator directly, so the whole module
# is in-process. Most tests reach it through the `eval_state` fixture, which
# the no-collector rule already finds; a few build an `EvalState` themselves,
# which it cannot. See nanopynix_testing.nix_runtime.
pytestmark = pytest.mark.evaluator_in_process

requires_boehm_gc = pytest.mark.nix_capability("boehm_gc")
"""Skip a test whose subject is the collector, on a build that has none.

nanopynix builds libexpr with ``-Dgc=disabled`` for the AddressSanitizer
variant, because libexpr refuses ASAN together with a conservative collector.
``_gc_stats`` and ``_gc_collect`` raise in such a build, and the behaviour
these tests measure -- a root coming back -- does not exist there. Every other
variant keeps the collector, so each of these still runs four ways.
"""


class TestEvalString:
    def test_eval_int(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1 + 2")
        assert v.type() == "int"
        assert v.as_int() == 3

    def test_eval_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("3.14")
        assert v.type() == "float"
        assert v.as_float() == 3.14

    def test_eval_bool_true(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("true")
        assert v.type() == "bool"
        assert v.as_bool() is True

    def test_eval_bool_false(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("false")
        assert v.type() == "bool"
        assert v.as_bool() is False

    def test_eval_string(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('"hello"')
        assert v.type() == "string"
        assert v.as_string() == "hello"

    def test_eval_null(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("null")
        assert v.type() == "null"
        # null is not a bool, and as_* raises on the wrong type. This used to
        # read as False; see is_null() for the lenient check.
        assert v.is_null() is True
        with pytest.raises(Exception, match="bool"):
            v.as_bool()

    def test_eval_nested_string_interpolation(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('"hello ${"world"}"')
        assert v.type() == "string"
        assert v.as_string() == "hello world"

    def test_value_keeps_eval_state_alive(self, store: Any, init_expr: object) -> None:
        eval_state = nanopynix.EvalState(store)
        value = eval_state.eval_string("42")

        del eval_state
        gc.collect()

        assert value.as_int() == 42

    def test_a_selected_value_keeps_the_eval_state_alive_without_its_parent(
        self, store: Any, init_expr: object
    ) -> None:
        """A child must hold the evaluator itself, not the value it came from.

        The parent is dropped here on purpose. ``attr_get`` used to keep it
        alive, and the evaluator only through it, which meant one child pinned
        every root above it. It now reaches the evaluator directly, and this
        is the guarantee that must survive that change.

        Nothing softer would do. A value that outlives its ``EvalState`` is
        not merely unusable: ``EvalMemory`` owns the AST arena and
        ``EvalState`` owns the symbol table, so a thunk would hold ``Expr *``
        into freed memory and an attrset would hold ``Symbol`` into a
        destroyed table.
        """
        eval_state = nanopynix.EvalState(store)
        parent = eval_state.eval_string("{ a = 1; }")
        child = parent.attr_get("a")

        del parent, eval_state
        gc.collect()

        assert child.as_int() == 1


class TestEvalAttrs:
    def test_eval_simple_attrs(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('{ a = 1; b = true; c = "hi"; }')
        assert v.type() == "attrs"
        assert v.has_attr("a")
        assert v.has_attr("b")
        assert v.has_attr("c")
        attrs = v.attr_names()
        assert "a" in attrs
        assert "b" in attrs
        assert "c" in attrs

    def test_attr_get(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{ x = 42; }")
        x = v.attr_get("x")
        assert x.as_int() == 42

    def test_attr_get_missing_raises(self, eval_state: nanopynix.EvalState):
        """Nix's own wording and Nix's own "Did you mean ...?" suggestions.

        This used to be ``std::runtime_error("attribute 'y' not found")`` --
        our phrasing, and no suggestions. It is now
        ``nanopynix::MissingAttributeError``, which says what Nix says for
        ``{ x = 1; }.y`` and ranks candidates with the same
        ``Suggestions::bestMatches``. Building it here rather than raising a
        ``KeyError`` from Python is the point: the candidate names are this
        attrset's symbol table, so C++ is the only place they exist.

        Nix colourises, so the quoted name has ANSI escapes around it -- hence
        matching on the words either side rather than on ``'y'``.
        """
        v = eval_state.eval_string("{ x = 1; }")
        with pytest.raises(RuntimeError, match=r"attribute .* missing") as excinfo:
            v.attr_get("y")
        assert "Did you mean" in str(excinfo.value)

    def test_attr_get_on_non_attrs_raises(self, eval_state: nanopynix.EvalState):
        """The wrong type is Nix's own TypeError, not a generic RuntimeError.

        This used to be ``RuntimeError("value is not an attribute set")`` from a
        hand-written ``v->type() != nAttrs`` check. Routing through nix's
        ``forceAttrs`` means the message names the type it did find, and the
        exception lands inside the NixError hierarchy where callers can catch
        it alongside every other type mismatch.
        """
        v = eval_state.eval_string("1")
        # Nix colourises its messages, so there are ANSI escapes between
        # "found" and the type name -- match only up to that point.
        with pytest.raises(Exception, match="expected a set but found") as excinfo:
            v.attr_get("x")
        assert type(excinfo.value).__name__ == "TypeError"


class TestEvalList:
    def test_eval_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[1 2 3]")
        assert v.type() == "list"
        assert v.list_length() == 3

    def test_list_get(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[10 20 30]")
        assert v.list_get(0).as_int() == 10
        assert v.list_get(1).as_int() == 20
        assert v.list_get(2).as_int() == 30

    def test_list_get_out_of_range_raises(self, eval_state: nanopynix.EvalState):
        """At the *binding* layer this is a Nix error, not yet an ``IndexError``.

        It used to be ``std::out_of_range``, which nanobind's default
        translator turns into ``IndexError``. It is now
        ``nanopynix::ListIndexError`` so that it can carry Nix's ``ErrorInfo``
        and pair with the missing-attribute case.

        Being an ``IndexError`` is a property of the *public* class,
        ``nanopynix.ListIndexError``, which the boundary-A translation
        produces; the bound classes deliberately have no relationship to the
        public hierarchy (the same fact ``tests/temp/test_error_matrix.py``
        pins for every other type). Callers get the Pythonic behaviour -- see
        ``tests/temp/test_exception_translation.py`` -- they just do not get it
        from ``nanopynix_bindings`` directly.
        """
        v = eval_state.eval_string("[10 20 30]")
        with pytest.raises(RuntimeError, match="out of bounds"):
            v.list_get(3)

    def test_list_nested(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[[1 2] [3 4]]")
        assert v.list_length() == 2
        inner = v.list_get(0)
        assert inner.list_length() == 2


class TestToPython:
    def test_int(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("42")
        assert v.to_python() == 42

    def test_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("3.14")
        assert v.to_python() == 3.14

    def test_bool(self, eval_state: nanopynix.EvalState):
        assert eval_state.eval_string("true").to_python() is True
        assert eval_state.eval_string("false").to_python() is False

    def test_string(self, eval_state: nanopynix.EvalState):
        assert eval_state.eval_string('"hello"').to_python() == "hello"

    def test_null(self, eval_state: nanopynix.EvalState):
        assert eval_state.eval_string("null").to_python() is None

    def test_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('[1 true "hi" null]')
        assert v.to_python() == [1, True, "hi", None]

    def test_attrs_to_dict(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('{ a = 1; b = [true false]; c = "hi"; }')
        assert v.to_python() == {"a": 1, "b": [True, False], "c": "hi"}

    def test_nested_attrs(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{ outer = { inner = 42; }; }")
        assert v.to_python() == {"outer": {"inner": 42}}


class TestCall:
    def test_call_function(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("x: x + 1")
        assert fn.type() == "function"
        result = fn.call(eval_state.eval_string("41"))
        assert result.as_int() == 42

    def test_call_multi_arg(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("x: y: x + y")
        mid = fn.call(eval_state.eval_string("10"))
        result = mid.call(eval_state.eval_string("32"))
        assert result.as_int() == 42

    def test_call_with_attrs(self, eval_state: nanopynix.EvalState):
        fn = eval_state.eval_string("{x, y}: x + y")
        arg = eval_state.eval_string("{ x = 40; y = 2; }")
        result = fn.call(arg)
        assert result.as_int() == 42


class TestValueTypes:
    def test_is_methods_int(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1")
        assert v.is_int()
        assert not v.is_float()
        assert not v.is_string()

    def test_is_methods_float(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1.5")
        assert v.is_float()
        assert not v.is_int()

    def test_is_methods_bool(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("true")
        assert v.is_bool()
        assert not v.is_int()

    def test_is_methods_string(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string('"x"')
        assert v.is_string()
        assert not v.is_attrs()

    def test_is_methods_attrs(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("{}")
        assert v.is_attrs()

    def test_is_methods_list(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("[]")
        assert v.is_list()

    def test_is_methods_function(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("x: x")
        assert v.is_function()

    def test_is_methods_null(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("null")
        assert v.is_null()


class TestForce:
    def test_force(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("1 + 2")
        v.force()  # Should not raise

    def test_force_deep(self, eval_state: nanopynix.EvalState):
        v = eval_state.eval_string("rec { a = 1 + 2; b = [(3 + 4)]; }")
        v.to_python()  # Should not raise


class TestEvalFile:
    def test_eval_file_simple(self, eval_state: nanopynix.EvalState, tmp_path: Path):
        nix_file = tmp_path / "test.nix"
        nix_file.write_text("42")
        result = nanopynix.eval_file(eval_state, str(nix_file))
        assert result.as_int() == 42


class TestAllocValue:
    def test_alloc_value(self, eval_state: nanopynix.EvalState):
        v = eval_state.alloc_value()
        assert v is not None
        # alloc'd values start as thunks/nulls — accessing type is fine
        assert isinstance(v.type(), str)


def _off_thread(work: Callable[[], Any]) -> Any:
    """Run ``work`` on a fresh thread, and give its result back here.

    An error travels back too, and this function raises it on the calling
    thread, so a test can use ``pytest.raises`` as usual.
    """
    outcome: list[tuple[str, Any]] = []

    def body() -> None:
        try:
            outcome.append(("value", work()))
        except Exception as error:
            # Broad on purpose, and nothing is hidden: the caller raises this
            # again below, on its own thread. A thread that dies of an
            # exception reports it nowhere the test can see.
            outcome.append(("error", error))

    thread = threading.Thread(target=body, name="foreign")
    thread.start()
    thread.join()
    kind, payload = outcome[0]
    if kind == "error":
        raise payload
    return payload


def _thread_ids_in(message: str) -> list[str]:
    """The thread identifiers the refusal names, in the order it names them.

    ``std::thread::id`` streams as the underlying ``pthread_t`` on glibc,
    which is the same number ``threading.get_ident()`` returns. The tests
    below compare the two. A libstdc++ that changes that representation must
    fail here rather than quietly stop naming anything a reader can act on.
    """
    return re.findall(r"thread (\d+)", message)


class TestThreadConfinement:
    """An evaluator belongs to the thread that built it, and says so.

    The Boehm collector neither scans nor suspends a thread it does not know,
    and an evaluator allocates in the collected heap, and writes pointers into
    it, on whichever thread drives it. So a foreign thread is not a rare race.
    It is a stack the collector cannot see.

    Before this guard a foreign thread forced thunks, allocated, and returned
    correct answers, which is the worst outcome available: the corruption is
    silent. See issue #30.
    """

    def test_a_foreign_thread_cannot_read_a_value(self, store: Any, init_expr: object) -> None:
        eval_state = nanopynix.EvalState(store)
        value = eval_state.eval_string("1 + 1")
        assert value.as_int() == 2

        caller_ident: list[int] = []

        def read_from_a_foreign_thread() -> int:
            caller_ident.append(threading.get_ident())
            return value.as_int()

        with pytest.raises(RuntimeError) as excinfo:
            _off_thread(read_from_a_foreign_thread)

        message = str(excinfo.value)
        note(refusal=message)
        owner, caller = _thread_ids_in(message)
        assert owner == str(threading.get_ident())
        assert caller == str(caller_ident[0])

    @pytest.mark.parametrize(
        "operation",
        [
            "type",
            "attr_names",
            "to_python",
            "force",
        ],
    )
    def test_every_accessor_refuses_a_foreign_thread(self, store: Any, init_expr: object, operation: str) -> None:
        """One funnel, so one guard covers all of them.

        Each accessor reaches ``PyValue::evalState`` through ``checkedValue``
        or ``requireEvalState``. These four take different routes through
        that funnel, so they are the sample that shows the funnel holds.
        """
        eval_state = nanopynix.EvalState(store)
        value = eval_state.eval_string("{ a = 1; }")

        with pytest.raises(RuntimeError, match="belongs to thread"):
            _off_thread(getattr(value, operation))

    def test_a_foreign_thread_cannot_drive_the_eval_state(self, store: Any, init_expr: object) -> None:
        eval_state = nanopynix.EvalState(store)

        with pytest.raises(RuntimeError, match="belongs to thread"):
            _off_thread(lambda: eval_state.eval_string("1"))

    def test_the_owner_is_the_building_thread_and_not_the_main_thread(self, store: Any, init_expr: object) -> None:
        """The rule is affinity, not "the main thread".

        The evaluator here is built on a worker thread, so that worker owns
        it, and this thread -- the main one -- is the foreign one.
        """
        built: list[Any] = []

        def build_and_read() -> int:
            eval_state = nanopynix.EvalState(store)
            built.append(eval_state)
            return eval_state.eval_string("7").as_int()

        assert _off_thread(build_and_read) == 7

        with pytest.raises(RuntimeError, match="belongs to thread"):
            built[0].eval_string("7")

    @requires_boehm_gc
    def test_a_value_dropped_on_a_foreign_thread_still_frees_its_root(self, store: Any, init_expr: object) -> None:
        """The destructor is exempt, on purpose.

        Since #11 the last reference to a value usually dies on whichever
        thread drops it, which is not the evaluator's thread. ``~PyValue``
        only frees a Boehm root, and ``GC_FREE`` needs no registration, so a
        guard there would break every caller and protect nothing.

        No collection is needed to see the memory come back. Nix allocates a
        root with ``traceable_allocator``, which is ``GC_MALLOC_UNCOLLECTABLE``
        to allocate and ``GC_FREE`` to release, so the counter of
        uncollectable bytes moves the moment the destructor runs.
        """
        eval_state = nanopynix.EvalState(store)
        before = nanopynix_expr._gc_stats()["non_gc_bytes"]  # type: ignore[reportPrivateUsage] -- L1 collector probe

        values = [eval_state.eval_string(str(n)) for n in range(200)]
        rooted = nanopynix_expr._gc_stats()["non_gc_bytes"]  # type: ignore[reportPrivateUsage] -- L1 collector probe
        assert rooted > before

        _off_thread(values.clear)
        gc.collect()
        after = nanopynix_expr._gc_stats()["non_gc_bytes"]  # type: ignore[reportPrivateUsage] -- L1 collector probe
        note(root_bytes_held=rooted - before, root_bytes_left=after - before)

        # Every root the 200 values held came back. The counter can end below
        # `before`, because `gc.collect()` also reaches values that earlier
        # tests left behind, so this is a ceiling and not an equality.
        assert after <= before
        assert eval_state.eval_string("1").as_int() == 1


class TestEvaluatorThreadRegistration:
    """`_enter_evaluator_thread` and `_exit_evaluator_thread` must always pair.

    Boehm stops the world by signalling every thread in its own `GC_threads`
    table. An entry that names a thread which has exited is a `pthread_kill`
    on a dead thread, which is issue #72 when glibc answers `EINVAL` and issue
    #53 when it faults instead. So the invariant the collector needs is that
    a registration never outlives the thread it names.

    The tests below drive the two hooks directly, on threads of their own,
    rather than through an evaluator. `NixThreadExecutor` calls them as its
    pool initializer and its thread finalizer, and a defect there would look
    like a defect of the whole executor.
    """

    def test_a_thread_registers_and_unregisters(self) -> None:
        """The pair works on a fresh thread, and leaves nothing behind."""

        def body() -> str:
            nanopynix_expr._enter_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            nanopynix_expr._exit_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            return "paired"

        assert _off_thread(body) == "paired"

    def test_the_pair_works_again_on_the_next_thread(self) -> None:
        """Ten threads in turn, each registering and unregistering.

        glibc caches thread stacks, so these threads share `pthread_t` values.
        A registration that survived its thread would make the next thread see
        `GC_DUPLICATE`, and the run would end with a collection signalling a
        thread that is gone. Reaching the assertion is what says it did not.
        """

        def body() -> str:
            nanopynix_expr._enter_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            nanopynix_expr._exit_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            return "paired"

        assert [_off_thread(body) for _ in range(10)] == ["paired"] * 10

    def test_registering_twice_on_one_thread_is_refused(self) -> None:
        """The guard that keeps two evaluators off one thread.

        It is also why a `GC_DUPLICATE` can never mean a live evaluator: an
        evaluator whose thread is already registered never reaches the
        collector call at all.
        """

        def body() -> None:
            nanopynix_expr._enter_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            try:
                with pytest.raises(RuntimeError, match="already registered"):
                    nanopynix_expr._enter_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            finally:
                nanopynix_expr._exit_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test

        _off_thread(body)

    def test_unregistering_without_registering_is_refused(self) -> None:
        """An unpaired exit says so, rather than unregistering someone else."""

        def body() -> None:
            with pytest.raises(RuntimeError, match="not registered"):
                nanopynix_expr._exit_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test

        _off_thread(body)

    @requires_boehm_gc
    def test_a_stale_registration_is_taken_over_and_removed(self, eval_state: nanopynix.EvalState) -> None:
        """A `GC_DUPLICATE` must not survive the thread it names. Issue #73.

        The `eval_state` fixture is what starts the collector, and it is not
        decoration. `GC_register_my_thread` aborts the process with "Threads
        explicit registering is not previously enabled" when the collector has
        not started, because `nix::initGC` is what calls
        `GC_allow_register_threads`. Without the fixture this test kills the
        run whenever it goes first -- measured, with `-k`.

        `GC_register_my_thread` answers `GC_DUPLICATE` in one case: a live
        entry already names this `pthread_t`. Every thread that registers is
        `DETACHED`, so unregistering removes its entry, and a live entry can
        therefore only belong to a thread that registered, exited without
        unregistering, and had its `pthread_t` handed on by glibc.

        The branch used to leave such an entry alone, on the belief that a
        Python runtime owned it. CPython links no collector and registers
        nothing. Leaving it makes the stale entry permanent, and
        `GC_suspend_all` signals every entry that is neither the caller nor
        `FINISHED` -- which is the call site of #72 and #53.

        So `enter` adopts it and `exit` removes it. The final assertion is the
        whole point: the entry is gone with the thread.
        """

        assert eval_state is not None, "the fixture starts the collector"

        def body() -> bool:
            # The state that glibc's stack reuse produces, made directly.
            nanopynix_expr._gc_register_this_thread_unowned()  # type: ignore[reportPrivateUsage] -- the tool under test
            assert nanopynix_expr._gc_thread_is_registered() is True, (  # type: ignore[reportPrivateUsage] -- the probe under test
                "the probe must see the entry it just made"
            )
            nanopynix_expr._enter_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            nanopynix_expr._exit_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook under test
            return nanopynix_expr._gc_thread_is_registered()  # type: ignore[reportPrivateUsage] -- the probe under test

        assert _off_thread(body) is False, (
            "the entry outlived the thread, so a collection will signal a thread that is gone"
        )

    @requires_boehm_gc
    @LINUX_PROC_FS
    def test_the_collector_has_an_owner_thread(self, eval_state: nanopynix.EvalState) -> None:
        """One thread starts the collector, and that thread is still running.

        Boehm gives its one static `first_thread` entry to whichever thread
        reaches `GC_thr_init`, and it never removes that entry. So the thread
        that starts the collector must outlive every collection.

        `Session.open` used to start it on a `nix-store` executor thread,
        which exits with the session. That left an entry naming a dead thread,
        and every later stop-the-world signalled it: issues #53, #69 and #72.
        Measured on one selection, one build: 3 crashes in 4 runs that way,
        and 0 crashes in 6 runs with an immortal thread.

        The owner belongs to the bindings, and not to any Python object. This
        fixture builds an `EvalState` directly, without a `Session`, and it
        must be as safe as the engines are.
        """
        assert eval_state is not None
        owner = nanopynix_expr._gc_owner_thread_id()  # type: ignore[reportPrivateUsage] -- the probe under test
        assert owner != 0, "the collector is up, so it has an owner thread"
        assert Path(f"/proc/self/task/{owner}").exists(), f"the collector's owner thread {owner} has exited"

    @requires_boehm_gc
    def test_the_main_thread_does_not_own_the_collector(self, eval_state: nanopynix.EvalState) -> None:
        """The collector does not come up at import, and must not.

        bdwgc installs no atfork handlers unless something calls
        `GC_set_handle_fork` before `GC_INIT`, and the default is off. A
        forkserver parent that only imports nanopynix must therefore not bring
        the collector up, or every worker child inherits a thread table that
        nothing fixes up.

        The owner is a thread of its own, so it is not this one.
        """
        assert eval_state is not None
        owner = nanopynix_expr._gc_owner_thread_id()  # type: ignore[reportPrivateUsage] -- the probe under test
        assert owner != threading.get_native_id(), "the collector must not come up on the main thread"

    @requires_boehm_gc
    def test_the_thread_that_builds_an_evaluator_is_registered(self, eval_state: nanopynix.EvalState) -> None:
        """The stack that holds the values must be a stack the collector scans.

        This fixture builds its evaluator with `nanopynix.EvalState(store)`, on
        the main thread, and never goes near `NixThreadExecutor`. That is the
        shape a library caller writes, and it used to leave the main thread
        absent from Boehm's thread table.

        `GC_push_all_stacks` walks that table and visits nothing else. An
        absent thread therefore aborts the process with "Collecting from
        unknown thread" when a collection starts on it, and silently loses the
        values that only its stack refers to when a collection starts
        somewhere else. Issue #70 recorded the second one for a long time.

        This assertion used to read `is False`, and it passed. The old
        expectation described the defect.
        """
        assert eval_state is not None
        assert nanopynix_expr._gc_thread_is_registered() is True  # type: ignore[reportPrivateUsage] -- the probe under test

    @requires_boehm_gc
    def test_a_plain_thread_is_not_registered(self, eval_state: nanopynix.EvalState) -> None:
        """The probe above reports the thread, and not the process.

        Without this, a probe that always answered False would pass the test
        above and say nothing.
        """

        def registered_here() -> bool:
            nanopynix_expr._enter_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook that must flip the probe
            try:
                return nanopynix_expr._gc_thread_is_registered()  # type: ignore[reportPrivateUsage] -- the probe under test
            finally:
                nanopynix_expr._exit_evaluator_thread()  # type: ignore[reportPrivateUsage] -- the hook that must flip the probe

        def registered_nowhere() -> bool:
            return nanopynix_expr._gc_thread_is_registered()  # type: ignore[reportPrivateUsage] -- the probe under test

        assert _off_thread(registered_here) is True
        assert _off_thread(registered_nowhere) is False


class TestValueDoc:
    def test_get_doc_builtin(self, eval_state: nanopynix.EvalState) -> None:
        v = eval_state.eval_string("builtins.add")
        doc = v.get_doc()
        assert doc is not None
        assert doc["name"] == "add"
        assert doc["args"] == ["e1", "e2"]
        assert doc["arity"] == 2
        assert "sum" in doc["doc"]

    def test_get_doc_lambda(self, eval_state: nanopynix.EvalState) -> None:
        v = eval_state.eval_string("/** Adds one */ x: x + 1")
        doc = v.get_doc()
        assert doc is not None
        assert "Function" in doc["doc"]
        assert "Adds one" in doc["doc"]

    def test_get_doc_scalar_returns_none(self, eval_state: nanopynix.EvalState) -> None:
        v = eval_state.eval_string("42")
        assert v.get_doc() is None

    def test_attr_doc(self, eval_state: nanopynix.EvalState, tmp_path: Path) -> None:
        nix_file = tmp_path / "attrs.nix"
        nix_file.write_text("{ /** First attribute */ foo = 123; bar = 456; }")
        v = eval_state.eval_file(str(nix_file))
        doc_foo = v.attr_doc("foo")
        assert doc_foo is not None
        assert doc_foo["doc"] is not None
        assert doc_foo["doc"].strip() == "First attribute"
        assert doc_foo["line"] == 1

        doc_bar = v.attr_doc("bar")
        assert doc_bar is not None
        assert doc_bar["doc"] is None

        assert v.attr_doc("nonexistent") is None

    def test_repl_select(self, eval_state: nanopynix.EvalState) -> None:
        eval_state.begin_repl()
        eval_state.repl_process_line("pkgs = { hello = 1; };")
        res = eval_state.repl_select("pkgs.hello")
        assert res is not None
        assert res["name"] == "hello"
        assert res["attrs"].is_attrs()

        assert eval_state.repl_select("1 + 2") is None
