"""Can a store implemented in Python actually serve path info?

``register_store_implementation`` lets a Python object stand in as a Nix store.
Until now the only thing that worked was ``is_valid_path``: every test in
``test_store_backend_registration.py`` returns ``None`` from
``query_path_info``, and returning anything else failed. Three separate defects
stacked up, each hiding the next:

1. ``py_store_impl.cpp`` never included nanobind's ``stl/string.h``, so there
   was no ``std::string`` type caster. ``nb::cast<std::string>`` still compiled
   -- it falls back to the bound-type path -- and threw at runtime, so every
   string-valued field failed.
2. ``PyStoreImpl`` did not keep its config alive. ``nix::Store`` holds its
   config as a bare ``const Config &`` (``store-api.hh:301``) and every
   concrete Nix store owns the ref itself; this one did not, so the config was
   freed when the constructor returned and later reads segfaulted in
   ``printStorePath`` or tripped ``AbstractSetting``'s ``created == 123``
   canary.
3. ``queryPathInfoUncached`` treated a key present-but-``None`` as a value and
   threw casting it. Since ``path_info_to_dict`` renders every absent optional
   as ``None`` rather than omitting the key, that meant the natural round-trip
   shape failed -- and the exception was swallowed by a ``catch`` that printed
   to stderr and returned the *underlying* store's answer instead.

The third one is why these tests assert on error propagation as well as on
values: a Python store that fails must say so, not quietly hand back someone
else's data.

A fourth defect of the same class sat in ``PyStoreConfig::openStore``: casting
``underlying_store`` needs nanobind's ``stl/shared_ptr.h``, which was also
missing, so ``open_store`` raised ``std::bad_cast`` and every ``if
(underlying)`` fallthrough in ``py_store_impl.cpp`` -- roughly half the file --
was unreachable. ``TestUnderlyingStoreFallthrough`` covers it.

Four further groups here are about the shape of the interface rather than that
first set of bugs. ``TestStoreImplIsRequired`` covers the move from ``hasattr``
probing to ``nanopynix.StoreImpl`` override detection.
``TestQueriesNoLongerAnswerForThePythonStore`` covers four queries that used to
invent an answer instead of asking the store or deferring to Nix.

``TestTheWholeDispatchableInterface`` covers the widening from three operations
to fifteen, and is deliberately written against the *list* rather than against
individual methods. The failure it exists to catch is not a wrong answer but a
dead one: a name that appears on both sides and is never wired up in between
looks implemented, type-checks, and does nothing. So it asserts that the two
sides agree on the list, that every name on it is both callable and dispatched,
and that neither side carries a name the other does not.

``TestEveryOperationDelegates`` covers the branch the other two leave out. Each
dispatch site is three-way -- Python, then ``underlying``, then ``nix::Store``
-- and those groups pin the first and the last; this one runs the middle for
every operation at once, by asserting a store that implements nothing answers
exactly as the store it delegates to. For five of the fifteen it *runs* the
middle without *pinning* it: see :data:`DELEGATION_INDISTINGUISHABLE_FROM_BASE`,
which names them and says, per operation, what was measured.

``TestTheUndispatchableCaseIsSaidOutLoud`` covers the one name on that list
that some builds cannot dispatch at all. There the dead method is unavoidable
-- Nix declares ``Store::readDerivation`` non-virtual before 2.32 -- so what is
pinned instead is that it is *announced*, by a warning at store-open time and a
``build_info()`` capability. Its two tests are version-marked rather than
runtime-branched, so a build that skipped one says so in the report.

Registration is process-global and cannot be undone, so each store here gets a
unique scheme; otherwise re-registering under a parametrized run would raise.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from nanopynix_bindings import store as nanopynix_store, util as nanopynix_util

import nanopynix
from nanopynix import LogCollector

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

_scheme_counter = itertools.count()

BOGUS = "00000000000000000000000000000000-bogus"

# A fully populated reply in exactly the shape `query_path_info` hands back,
# `None`s included. Every field here was either unread or fatal before the fix.
FULL_INFO = {
    "path": f"/nix/store/{BOGUS}",
    "references": ["/nix/store/11111111111111111111111111111111-ref"],
    "nar_hash": "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
    "nar_size": 4242,
    "registration_time": 1700000000,
    "deriver": "/nix/store/22222222222222222222222222222222-d.drv",
    "ca": None,
    "ultimate": True,
    "sigs": [
        "cache.example-1:x2ZJcaVvSvJqmYWzOgYRDCRSlZ0IF0nQ9RFCzOhBrjPnzsMLRfClXKN1PMbHFtHvB6Y2Q6XkQqXtcW0BQmYPCw=="
    ],
}


def register_and_open(python_store_cls: type[Any]) -> Any:
    """Register ``python_store_cls`` under a fresh scheme and open it."""
    scheme = f"pytest-pystore-{next(_scheme_counter)}"

    class Factory:
        @staticmethod
        def open_store() -> object:
            return python_store_cls()

    nanopynix_store.register_store_implementation(scheme, scheme, [scheme], Factory())
    return nanopynix.open_store(f"{scheme}://example")


def open_python_store(query_path_info: Callable[[str], Any]) -> Any:
    """Register a one-off Python store whose query_path_info is ``query_path_info``."""

    class PythonStore(nanopynix.StoreImpl):
        def is_valid_path_uncached(self, path: str) -> bool:
            return True

        def query_path_info(self, path: str) -> Any:
            return query_path_info(path)

        def query_path_from_hash_part(self, hash_part: str) -> None:
            return None

    return register_and_open(PythonStore)


def test_the_store_config_outlives_the_constructor() -> None:
    """Reading anything off the config used to read freed memory.

    ``get_store_dirs`` is the cheapest thing that touches the config, and it
    used to abort the process outright -- ``AbstractSetting``'s destructor
    canary, not a catchable error -- so this passing at all is the assertion.
    """
    store = open_python_store(lambda _path: None)

    dirs = store.get_store_dirs()

    assert dirs["store_dir"] == "/nix/store"


def test_query_path_info_reports_what_the_python_store_returned() -> None:
    store = open_python_store(lambda _path: dict(FULL_INFO))

    info = store.query_path_info(nanopynix_store.StorePath(BOGUS))

    assert info["nar_size"] == 4242
    assert info["nar_hash"] == FULL_INFO["nar_hash"]
    assert info["references"] == FULL_INFO["references"]
    assert info["registration_time"] == 1700000000


@pytest.mark.parametrize("field", ["deriver", "ultimate", "sigs"])
def test_fields_the_reader_never_looked_at(field: str) -> None:
    """``deriver``, ``ultimate`` and ``sigs`` were silently dropped on the way in.

    Split per field so a regression names the field it lost rather than failing
    one big assertion. ``sigs`` is the interesting one: 2.31 stores signatures
    as plain strings and 2.34+ as parsed ``nix::Signature``, so it goes through
    a version-guarded ``Signature::parse``.
    """
    store = open_python_store(lambda _path: dict(FULL_INFO))

    info = store.query_path_info(nanopynix_store.StorePath(BOGUS))

    assert info[field] == FULL_INFO[field]


def test_absent_optionals_may_be_spelled_none() -> None:
    """The shape query_path_info itself produces must be accepted back.

    ``path_info_to_dict`` renders an absent optional as an explicit ``None``,
    so a store echoing that shape sends ``registration_time``/``deriver``/``ca``
    as ``None``. Reading them as values threw, and the throw was swallowed --
    the caller got the underlying store's answer, or an unrelated error.
    """
    reply = dict(FULL_INFO, registration_time=None, deriver=None, ca=None)
    store = open_python_store(lambda _path: reply)

    info = store.query_path_info(nanopynix_store.StorePath(BOGUS))

    assert info["registration_time"] is None
    assert info["deriver"] is None
    assert info["ca"] is None
    # ... and the fields either side of the None ones still arrived.
    assert info["nar_size"] == 4242
    assert info["ultimate"] is True


def test_references_and_deriver_accept_base_names() -> None:
    """Both spellings work, because the protocol itself uses both.

    A Python store is *handed* base names (``StorePath::to_string()``), so
    echoing its input yields base names, while mirroring ``path_info_to_dict``
    yields full ``/nix/store/...`` paths. Accepting only one would make one of
    those two obvious implementations silently wrong.
    """
    reply = dict(
        FULL_INFO,
        references=["11111111111111111111111111111111-ref"],
        deriver="22222222222222222222222222222222-d.drv",
    )
    store = open_python_store(lambda _path: reply)

    info = store.query_path_info(nanopynix_store.StorePath(BOGUS))

    assert info["references"] == ["/nix/store/11111111111111111111111111111111-ref"]
    assert info["deriver"] == "/nix/store/22222222222222222222222222222222-d.drv"


def test_a_python_store_that_raises_surfaces_its_own_error() -> None:
    """No silent fallback, and the Python exception survives the C++ round trip.

    This is the defect that made the others invisible: the old handler caught
    everything, printed one line to stderr, and returned the underlying store's
    answer as though the Python store had produced it.
    """

    def explode(_path: str) -> Any:
        raise ValueError("boom from python store")

    store = open_python_store(explode)

    with pytest.raises(ValueError, match="boom from python store"):
        store.query_path_info(nanopynix_store.StorePath(BOGUS))


def test_returning_none_still_means_not_valid() -> None:
    """The one case that already worked keeps working."""
    store = open_python_store(lambda _path: None)

    with pytest.raises(Exception, match="is not valid"):
        store.query_path_info(nanopynix_store.StorePath(BOGUS))


@pytest.mark.parametrize(
    "returned",
    [f"/nix/store/{BOGUS}", BOGUS],
    ids=["full-path", "base-name"],
)
def test_query_path_from_hash_part_reaches_the_python_store(returned: str) -> None:
    """The third method that casts a string, and so was broken the same way.

    It has never had a test that returned anything but ``None``, which is why
    the dead ``std::string`` caster went unnoticed here too.
    """

    class PythonStore(nanopynix.StoreImpl):
        def is_valid_path_uncached(self, path: str) -> bool:
            return True

        def query_path_info(self, path: str) -> None:
            return None

        def query_path_from_hash_part(self, hash_part: str) -> str:
            return returned

    store = register_and_open(PythonStore)

    found = store.query_path_from_hash_part("00000000000000000000000000000000")

    assert found is not None
    assert found.to_string() == BOGUS


def test_real_path_info_round_trips_through_a_python_store(store: Any, store_seeded_path: Any) -> None:
    """The end-to-end claim: what one store reports, another can serve.

    Uses a real store's answer verbatim rather than a handcrafted dict, so it
    stays honest if the rendering gains a field -- which is exactly how the
    ``None``-valued keys got missed in the first place.

    Which asserts carry weight here, since a locally added path is sparse:
    ``ca`` is the load-bearing one -- it is populated (``fixed:sha256:...``)
    and ``FULL_INFO`` deliberately leaves it ``None``, so this is the only
    place a real ``ca`` makes the round trip. ``nar_hash``/``nar_size`` are
    populated too. ``references`` and ``sigs`` are ``[]`` here and ``deriver``
    is ``None``; those comparisons are trivially true, and their populated
    cases live in ``test_query_path_info_reports_what_the_python_store_returned``
    and ``test_fields_the_reader_never_looked_at``. They stay because a
    regression that made an empty list or a ``None`` *throw* would still be
    caught -- which is precisely the defect this file exists for.
    """
    original = store.query_path_info(store_seeded_path)
    served = open_python_store(lambda _path: dict(original))

    info = served.query_path_info(store_seeded_path)

    assert info["nar_hash"] == original["nar_hash"]
    assert info["nar_size"] == original["nar_size"]
    assert info["references"] == original["references"]
    assert info["deriver"] == original["deriver"]
    assert info["ca"] == original["ca"]
    assert info["sigs"] == original["sigs"]


class TestUnderlyingStoreFallthrough:
    """A Python store may delegate to a real one by exposing ``underlying_store``.

    ``PyStoreImpl`` guards nearly every method with ``if (underlying) return
    underlying->...``, so this is meant to be the way you write a store that
    overrides one or two operations and inherits the rest. None of it ran:
    ``openStore`` casts the attribute with ``nb::cast<std::shared_ptr<nix::Store>>``
    and nanobind's ``stl/shared_ptr.h`` was not included, so merely *opening*
    such a store raised ``RuntimeError: std::bad_cast``. No test had ever set
    the attribute, so the whole branch was dead in a way nothing detected.
    """

    def test_opening_a_store_with_an_underlying_store_works_at_all(self, store: Any) -> None:
        """The whole feature used to die here, before any method was called."""

        class Delegating(nanopynix.StoreImpl):
            underlying_store = store

        assert isinstance(register_and_open(Delegating), nanopynix_store.Store)

    def test_unimplemented_methods_reach_the_underlying_store(
        self, store: Any, store_seeded_path: Any
    ) -> None:
        """A store that implements nothing still answers, via the real one.

        Both assertions matter: ``is_valid_path`` would be ``False`` and
        ``query_path_info`` would raise "is not valid" if the fallthrough were
        skipped rather than taken, so neither can pass by accident.
        """

        class Delegating(nanopynix.StoreImpl):
            underlying_store = store

        delegating = register_and_open(Delegating)

        assert delegating.is_valid_path(store_seeded_path) is True
        assert delegating.query_path_info(store_seeded_path)["nar_size"] == (
            store.query_path_info(store_seeded_path)["nar_size"]
        )

    def test_an_implemented_method_wins_over_the_underlying_store(
        self, store: Any, store_seeded_path: Any
    ) -> None:
        """Delegation is a fallback, not an override -- otherwise it is useless.

        The seeded path really is valid in the underlying store, so a ``False``
        here can only have come from the Python method.
        """

        class Delegating(nanopynix.StoreImpl):
            underlying_store = store

            def is_valid_path_uncached(self, path: str) -> bool:
                return False

        assert register_and_open(Delegating).is_valid_path(store_seeded_path) is False

    def test_underlying_store_may_be_set_per_instance(self, store: Any, store_seeded_path: Any) -> None:
        """Setting it in ``__init__`` is the realistic pattern, so it must type-check.

        Every other test here assigns at class level, which is what let
        ``underlying_store`` sit annotated as a ``ClassVar`` -- an annotation
        that makes ``self.underlying_store = ...`` an error for exactly the
        usage the docs recommend. Runtime never cared; pyright would have, on
        the first real store anybody wrote.
        """

        class Delegating(nanopynix.StoreImpl):
            def __init__(self) -> None:
                self.underlying_store = store

        assert register_and_open(Delegating).is_valid_path(store_seeded_path) is True

    def test_the_deferring_queries_still_delegate(self, store: Any, store_seeded_path: Any) -> None:
        """The three formerly-lying queries kept their delegation.

        They were fixed by replacing their invented answers with ``nix::Store``'s,
        which was nearly done by deleting them outright -- but each one also
        carried the ``underlying`` branch, and ``nix::Store`` knows nothing about
        ``underlying``. Deleting them would have silently dropped delegation, so
        this pins it: an enumerating store must still enumerate through the
        Python one.
        """

        class Delegating(nanopynix.StoreImpl):
            underlying_store = store

        delegating = register_and_open(Delegating)

        assert store_seeded_path.to_string() in [
            p.split("/")[-1] for p in delegating.query_all_valid_paths()
        ]


class TestStoreImplIsRequired:
    """Detection is by override, not by ``hasattr``.

    The old probe could not tell an implementation from a typo: misspell
    ``query_path_info`` and the store silently answered from the fallback. It
    also ran on every single call. ``StoreImpl`` records what a subclass really
    replaced, once, when the class is defined.
    """

    def test_a_duck_typed_store_is_rejected_with_a_useful_message(self) -> None:
        """The one place dropping duck typing is visible, so the error must teach."""

        class NotAStoreImpl:
            def is_valid_path_uncached(self, path: str) -> bool:
                return True

        with pytest.raises(TypeError, match=r"must subclass nanopynix\.StoreImpl"):
            register_and_open(NotAStoreImpl)

    def test_only_overridden_methods_are_recorded(self) -> None:
        """A method left alone is not an implementation, and a typo is not either."""

        class Partial(nanopynix.StoreImpl):
            def is_valid_path_uncached(self, path: str) -> bool:
                return True

        assert Partial._nanopynix_store_overrides == frozenset({"is_valid_path_uncached"})

    def test_an_unimplemented_method_is_not_dispatched_to(self) -> None:
        """``StoreImpl.query_path_info`` raising must not reach the caller.

        A subclass that does not override it has not implemented it, so the
        trampoline must skip it rather than call the base and surface
        ``NotImplementedError``.
        """

        class Partial(nanopynix.StoreImpl):
            def is_valid_path_uncached(self, path: str) -> bool:
                return True

        store = register_and_open(Partial)

        with pytest.raises(Exception, match="is not valid"):
            store.query_path_info(nanopynix_store.StorePath(BOGUS))


class TestQueriesNoLongerAnswerForThePythonStore:
    """Three queries used to invent an answer instead of asking or deferring.

    They are a different defect class from the rest of this file: not "a Python
    store cannot return data" but "a Python store returns data that is wrong",
    and wrong in the most dangerous direction -- claiming paths exist or are
    substitutable when the store says otherwise.
    """

    @staticmethod
    def _nothing_is_valid() -> Any:
        class NothingValid(nanopynix.StoreImpl):
            def is_valid_path_uncached(self, path: str) -> bool:
                return False

            def query_path_info(self, path: str) -> None:
                return None

        return register_and_open(NothingValid)

    def test_substitutable_paths_does_not_claim_everything_is_substitutable(self) -> None:
        """Used to return its argument verbatim, contradicting the store itself."""
        store = self._nothing_is_valid()
        path = nanopynix_store.StorePath(BOGUS)

        assert store.is_valid_path(path) is False
        assert list(store.query_substitutable_paths([path])) == []

    def test_all_valid_paths_reports_unsupported_rather_than_an_empty_store(self) -> None:
        """"Empty" and "cannot enumerate" are different answers.

        Returning ``[]`` made them indistinguishable to a caller. ``nix::Store``
        declares this ``unsupported`` (``store-api.hh:404`` on 2.34), which is
        the honest answer for a store with no way to enumerate itself.
        """
        store = self._nothing_is_valid()

        with pytest.raises(Exception, match="not supported"):
            store.query_all_valid_paths()

    def test_referrers_reports_unsupported_rather_than_no_referrers(self) -> None:
        """The fourth query of this class, found while widening the interface.

        It used to return silently without touching ``referrers``, which reads
        as "nothing refers to this path" -- the answer that makes it safe to
        delete. ``nix::Store`` declares it ``unsupported`` (``store-api.hh:476``
        on 2.34), and a garbage collector deserves to hear that.
        """
        store = self._nothing_is_valid()

        with pytest.raises(Exception, match="not supported"):
            store.query_referrers(nanopynix_store.StorePath(BOGUS))

    def test_derivation_outputs_asks_the_store_rather_than_answering(self) -> None:
        """The one deferral that recurses back into Python, so it gets its own test.

        ``nix::Store::queryDerivationOutputs`` reads the derivation, which needs
        ``queryPathInfo`` -- which dispatches straight back here. The concern is
        that the round trip ends legibly rather than in an abort or a confusing
        failure from a deeper frame: the store says the path is not valid, and
        that is exactly what the caller is told, naming the path.
        """
        store = self._nothing_is_valid()

        with pytest.raises(Exception, match="does not exist"):
            store.query_derivation_outputs(nanopynix_store.StorePath(f"{BOGUS}.drv"))


# How to invoke each dispatchable operation through the bound ``Store`` API.
#
# The keys are checked against `DISPATCHABLE_METHODS` below, so this table
# cannot fall behind the interface: adding an operation without a way to call it
# fails the test rather than going untested.
INVOCATIONS: dict[str, Callable[[Any], Any]] = {
    "is_valid_path_uncached": lambda s: s.is_valid_path(nanopynix_store.StorePath(BOGUS)),
    "query_path_info": lambda s: s.query_path_info(nanopynix_store.StorePath(BOGUS)),
    "query_path_from_hash_part": lambda s: s.query_path_from_hash_part("0" * 32),
    "query_referrers": lambda s: s.query_referrers(nanopynix_store.StorePath(BOGUS)),
    "query_valid_derivers": lambda s: s.query_valid_derivers(nanopynix_store.StorePath(BOGUS)),
    "query_derivation_outputs": lambda s: s.query_derivation_outputs(
        nanopynix_store.StorePath(BOGUS)
    ),
    "query_all_valid_paths": lambda s: s.query_all_valid_paths(),
    "query_substitutable_paths": lambda s: s.query_substitutable_paths(
        [nanopynix_store.StorePath(BOGUS)]
    ),
    "add_temp_root": lambda s: s.add_temp_root(nanopynix_store.StorePath(BOGUS)),
    "ensure_path": lambda s: s.ensure_path(nanopynix_store.StorePath(BOGUS)),
    "optimise_store": lambda s: s.optimise_store(),
    "verify_store": lambda s: s.verify_store(False, False),
    # The bound `Store` takes one path here and the dispatched virtual takes a
    # set -- Nix declares both overloads and only the set one is virtual.
    "compute_fs_closure": lambda s: s.compute_fs_closure(
        nanopynix_store.StorePath(BOGUS), False, False, False
    ),
    # `^*` rather than a bare path: a DerivedPath is a sum type, and a bare
    # `.drv` parses as "fetch this file" instead of "build it".
    "query_missing": lambda s: s.query_missing([f"/nix/store/{BOGUS}.drv^*"]),
    "read_derivation": lambda s: s.read_derivation(nanopynix_store.StorePath(f"{BOGUS}.drv")),
}

# Operations the interface advertises everywhere but only *dispatches* on some
# builds, and the `build_info()` capability that says which.
#
# There is exactly one, and it is not a soft spot in the interface: keeping
# `read_derivation` in `DISPATCHABLE_METHODS` on every version is what makes a
# 2.31 store's override visible at all -- `__init_subclass__` only records names
# that list contains. The gate belongs here rather than as a skip on the whole
# sweep, so that the other fourteen operations stay covered on 2.31.
CAPABILITY_GATED: dict[str, str] = {"read_derivation": "store_impl_read_derivation"}


def _dispatch_is_available(name: str) -> bool:
    capability = CAPABILITY_GATED.get(name)
    if capability is None:
        return True
    info: Any = nanopynix.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- build_info from C++ extension has no stubs
    return cast("dict[str, bool]", info["capabilities"])[capability]


# A derivation in Nix's own ATerm spelling, which is what a Python store returns
# from `read_derivation`.
#
# `__json` is deliberately present in the env list: Nix's parser routes it *out*
# of `env` and into `structuredAttrs`, so a reply that still carries it in `env`
# would prove the text was never parsed. That makes this string double as
# evidence that the value went through `parseDerivation` rather than through any
# reconstruction of our own.
RECORDED_DRV_OUT = "11111111111111111111111111111111-bogus-out"
RECORDED_DRV_ATERM = (
    "Derive("
    f'[("out","/nix/store/{RECORDED_DRV_OUT}","","")],'
    "[],[],"
    '"x86_64-linux","/bin/sh",["-c","true"],'
    '[("__json","{\\"hardening\\":{\\"format\\":false}}"),'
    f'("builder","/bin/sh"),("out","/nix/store/{RECORDED_DRV_OUT}")]'
    ")"
)


class RecordingStore(nanopynix.StoreImpl):
    """Implements every dispatchable operation and records which ones ran.

    Written out longhand rather than generated, because this is also the worked
    example of what a store implementing the whole interface looks like -- and
    because generating the methods would have to inject them into the class body
    to be detected at all, which is the one thing ``StoreImpl`` documents you
    cannot do afterwards.
    """

    calls: ClassVar[list[str]] = []
    # What `query_missing` was handed, kept so a test can check the derived-path
    # spelling survives the trip rather than only that the call arrived.
    query_missing_targets: ClassVar[list[str]] = []

    def _record(self, name: str) -> None:
        RecordingStore.calls.append(name)

    def is_valid_path_uncached(self, path: str) -> bool:
        self._record("is_valid_path_uncached")
        return True

    def query_path_info(self, path: str) -> dict[str, Any]:
        self._record("query_path_info")
        return dict(FULL_INFO)

    def query_path_from_hash_part(self, hash_part: str) -> str:
        self._record("query_path_from_hash_part")
        return BOGUS

    def query_referrers(self, path: str) -> Iterable[str]:
        self._record("query_referrers")
        return ["11111111111111111111111111111111-ref"]

    def query_valid_derivers(self, path: str) -> Iterable[str]:
        self._record("query_valid_derivers")
        return ["22222222222222222222222222222222-d.drv"]

    def query_derivation_outputs(self, path: str) -> Iterable[str]:
        self._record("query_derivation_outputs")
        return ["33333333333333333333333333333333-out"]

    def query_all_valid_paths(self) -> Iterable[str]:
        self._record("query_all_valid_paths")
        return [BOGUS]

    def query_substitutable_paths(self, paths: Iterable[str]) -> Iterable[str]:
        self._record("query_substitutable_paths")
        return list(paths)

    def add_temp_root(self, path: str) -> None:
        self._record("add_temp_root")

    def ensure_path(self, path: str) -> None:
        self._record("ensure_path")

    def optimise_store(self) -> None:
        self._record("optimise_store")

    def verify_store(self, check_contents: bool, repair: bool) -> bool:
        self._record("verify_store")
        return True

    def compute_fs_closure(
        self,
        paths: Iterable[str],
        flip_direction: bool,
        include_outputs: bool,
        include_derivers: bool,
    ) -> Iterable[str]:
        self._record("compute_fs_closure")
        return [*paths, "44444444444444444444444444444444-dep"]

    def query_missing(self, targets: Iterable[str]) -> dict[str, Any]:
        self._record("query_missing")
        RecordingStore.query_missing_targets = list(targets)
        return {
            "will_build": ["55555555555555555555555555555555-build.drv"],
            "will_substitute": ["66666666666666666666666666666666-sub"],
            "unknown": [],
            "download_size": 1234,
            "nar_size": 5678,
        }

    def read_derivation(self, path: str) -> str:
        self._record("read_derivation")
        return RECORDED_DRV_ATERM


class TestTheWholeDispatchableInterface:
    """Every operation the interface advertises must actually be dispatched.

    The interface used to be three operations named in C++ call sites and
    restated in Python. A name could be added on either side and do nothing:
    Python would define a method the trampoline never called, or C++ would look
    for one the base class never declared. Neither failed, they just silently
    did not work -- which is how the widened interface has to be tested, not
    one method at a time.
    """

    def test_the_two_sides_agree_on_what_is_dispatchable(self) -> None:
        """``StoreImpl`` derives its list from the trampoline rather than repeating it."""
        assert nanopynix.DISPATCHABLE_METHODS == nanopynix_store.STORE_DISPATCH_METHODS

    def test_every_dispatchable_name_is_a_method_on_store_impl(self) -> None:
        """A name C++ dispatches with no method to document it is unusable."""
        missing = [
            name
            for name in nanopynix.DISPATCHABLE_METHODS
            if not callable(getattr(nanopynix.StoreImpl, name, None))
        ]

        assert missing == []

    def test_store_impl_declares_no_method_that_is_never_dispatched(self) -> None:
        """The other direction: a method nothing calls looks implemented and is not."""
        declared = {
            name
            for name in vars(nanopynix.StoreImpl)
            if not name.startswith("_") and callable(vars(nanopynix.StoreImpl)[name])
        }

        assert declared == set(nanopynix.DISPATCHABLE_METHODS)

    def test_the_invocation_table_covers_the_interface(self) -> None:
        """Guards the test below: an untested operation must fail, not be skipped."""
        assert set(INVOCATIONS) == set(nanopynix.DISPATCHABLE_METHODS)

    def test_calling_each_operation_reaches_the_python_store(self) -> None:
        """The real claim: none of the advertised operations is dead.

        The record is cleared before each call, so an operation is only credited
        for a dispatch its own invocation caused -- several of these route
        through others in ``nix::Store``, and a shared tally would let a dead
        dispatch pass on someone else's call. Collected rather than asserted in
        the loop so a failure names every operation that never arrived, which is
        what a wiring mistake looks like: a whole group missing, not one.

        A raise is swallowed for that same reason, and only here. An operation
        whose dispatch is dead does not necessarily return quietly -- it falls
        through to ``nix::Store``, and several of those fail on a store with no
        real backing. Letting the exception out would end the sweep at the first
        casualty and report it as an error rather than as a list, which is what
        this test exists not to do. Whether an operation *answers* is asserted
        by its own test just below; the only claim here is that it arrived.

        Operations in ``CAPABILITY_GATED`` are passed over on a build that
        cannot dispatch them, rather than skipping the sweep: "this Nix has no
        hook for it" and "the hook is wired to nothing" are different claims,
        and only the second is a defect.
        """
        store = register_and_open(RecordingStore)

        never_dispatched: list[str] = []
        for name, invoke in INVOCATIONS.items():
            if not _dispatch_is_available(name):
                continue
            RecordingStore.calls = []
            outcome(lambda i=invoke: i(store))
            if name not in RecordingStore.calls:
                never_dispatched.append(name)

        assert never_dispatched == []

    def test_answers_come_back_from_the_python_store(self) -> None:
        """Dispatching is not enough; the reply has to survive the return trip.

        The set-valued queries all go through one marshalling helper, so one
        assertion each is enough to show the helper works in both directions --
        and ``query_substitutable_paths`` additionally proves the *inbound*
        direction, since it can only echo paths it was handed.
        """
        RecordingStore.calls = []
        store = register_and_open(RecordingStore)
        path = nanopynix_store.StorePath(BOGUS)

        assert store.query_referrers(path) == [
            "/nix/store/11111111111111111111111111111111-ref"
        ]
        assert store.query_valid_derivers(path) == [
            "/nix/store/22222222222222222222222222222222-d.drv"
        ]
        assert store.query_derivation_outputs(path) == [
            "/nix/store/33333333333333333333333333333333-out"
        ]
        assert store.query_all_valid_paths() == [f"/nix/store/{BOGUS}"]
        assert store.query_substitutable_paths([path]) == [f"/nix/store/{BOGUS}"]
        assert store.verify_store(False, False) is True

    def test_the_closure_the_python_store_computed_is_the_one_returned(self) -> None:
        """``compute_fs_closure`` is the only operation with an out-param to fill.

        Nix passes a set in and expects it added to rather than replaced, so
        this checks both halves arrive: the path the caller asked about, which
        the store echoed from its argument, and the one the store contributed.
        """
        RecordingStore.calls = []
        store = register_and_open(RecordingStore)

        closure = store.compute_fs_closure(nanopynix_store.StorePath(BOGUS), False, False, False)

        assert sorted(closure) == [
            f"/nix/store/{BOGUS}",
            "/nix/store/44444444444444444444444444444444-dep",
        ]

    def test_query_missing_round_trips_a_whole_reply(self) -> None:
        """The only operation answering with a dict rather than paths or a scalar.

        Both directions matter and neither is exercised elsewhere: the targets
        must reach Python as Nix's own ``^`` derived-path spelling -- lowering
        them to plain store paths would silently turn "build this" into "fetch
        this" -- and all five reply fields must come back, since a field the
        reader forgets is invisible rather than fatal. That is precisely how
        ``deriver``, ``ultimate`` and ``sigs`` were lost from ``query_path_info``.
        """
        RecordingStore.calls = []
        RecordingStore.query_missing_targets = []
        store = register_and_open(RecordingStore)

        missing = store.query_missing([f"/nix/store/{BOGUS}.drv^*"])

        assert RecordingStore.query_missing_targets == [f"/nix/store/{BOGUS}.drv^*"]
        assert missing["will_build"] == ["/nix/store/55555555555555555555555555555555-build.drv"]
        assert missing["will_substitute"] == ["/nix/store/66666666666666666666666666666666-sub"]
        assert missing["unknown"] == []
        assert missing["download_size"] == 1234
        assert missing["nar_size"] == 5678

    def test_a_partial_query_missing_reply_keeps_nix_s_defaults(self) -> None:
        """A store that only knows some fields should not have to invent the rest.

        The alternative -- requiring all five -- would push stores into making
        up a ``download_size``, which is the invented-answer problem this whole
        interface was reworked to avoid.
        """

        class KnowsOnlyWhatItWillBuild(nanopynix.StoreImpl):
            def query_missing(self, targets: Iterable[str]) -> dict[str, Any]:
                return {"will_build": [f"{BOGUS}.drv"]}

        missing = register_and_open(KnowsOnlyWhatItWillBuild).query_missing(
            [f"/nix/store/{BOGUS}.drv^*"]
        )

        assert missing["will_build"] == [f"/nix/store/{BOGUS}.drv"]
        assert missing["will_substitute"] == []
        assert missing["unknown"] == []
        assert missing["download_size"] == 0
        assert missing["nar_size"] == 0

    def test_any_iterable_is_an_acceptable_answer(self) -> None:
        """A set and a generator are both natural ways to answer these queries.

        Requiring a list would be a trap that only shows up at runtime, in C++,
        as a cast failure -- the exact failure mode this whole file exists for.
        """

        class Iterables(nanopynix.StoreImpl):
            def query_all_valid_paths(self) -> Iterable[str]:
                return iter([BOGUS])

            def query_valid_derivers(self, path: str) -> Iterable[str]:
                return {"22222222222222222222222222222222-d.drv"}

        store = register_and_open(Iterables)

        assert store.query_all_valid_paths() == [f"/nix/store/{BOGUS}"]
        assert store.query_valid_derivers(nanopynix_store.StorePath(BOGUS)) == [
            "/nix/store/22222222222222222222222222222222-d.drv"
        ]

    def test_a_raise_from_any_operation_reaches_the_caller(self) -> None:
        """Same guarantee the original three had, extended to the new ones."""

        class Explodes(nanopynix.StoreImpl):
            def query_valid_derivers(self, path: str) -> Iterable[str]:
                raise ValueError("boom from query_valid_derivers")

        store = register_and_open(Explodes)

        with pytest.raises(ValueError, match="boom from query_valid_derivers"):
            store.query_valid_derivers(nanopynix_store.StorePath(BOGUS))

    def test_an_implemented_operation_is_not_second_guessed_by_underlying(
        self, store: Any, store_seeded_path: Any
    ) -> None:
        """A Python answer is final, even when it is empty or ``None``.

        ``query_path_from_hash_part`` was the one operation that broke this: a
        ``None`` fell through to ``underlying_store``, so a store saying "I have
        no such path" was overruled by one that did. The other two treated the
        Python reply as final, which made the odd one out a bug rather than a
        choice.
        """
        hash_part = store_seeded_path.to_string().split("-")[0]

        class SaysNo(nanopynix.StoreImpl):
            underlying_store = store

            def query_path_from_hash_part(self, hash_part: str) -> None:
                return None

        # The underlying store really can resolve it, so a non-None answer here
        # could only have come from the fallthrough.
        assert store.query_path_from_hash_part(hash_part) is not None
        assert register_and_open(SaysNo).query_path_from_hash_part(hash_part) is None


class TestReadDerivationIsServedByNixSOwnParser:
    """The one operation answered with a serialization instead of a rendering.

    Every other method here returns the same dict/list shape ``nix_store.cpp``
    renders outward. ``read_derivation`` returns the ``.drv``'s ATerm text and
    lets ``parseDerivation`` read it, because rebuilding a ``nix::Derivation``
    from a dict would mean reimplementing five ``DerivationOutput`` variants, a
    ``DerivedPathMap`` tree and ``structuredAttrs`` -- each of which changed
    shape across the supported Nix versions.

    It is also an addition rather than a relaxation: before this, a Python store
    could not serve derivations *at all*. ``nix::Store::readDerivation`` does not
    route through ``query_path_info``; it reads through a store accessor, which
    a store with no ``underlying_store`` answers with an empty one.
    :meth:`test_a_store_that_does_not_implement_it_still_cannot_serve_one` pins
    that, so the claim stays measured rather than remembered.
    """

    def test_the_reply_is_parsed_rather_than_copied(self) -> None:
        """``__json`` moving out of ``env`` is the proof Nix's parser ran.

        Nix's ATerm reader lifts ``__json`` into ``structuredAttrs`` and erases
        it from ``env``. No plausible pass-through does that, so its absence
        from ``env`` distinguishes "parsed" from "handed back", which asserting
        on ``builder`` or ``system`` alone would not.
        """
        if not _dispatch_is_available("read_derivation"):
            pytest.skip("this Nix cannot dispatch read_derivation")
        RecordingStore.calls = []
        store = register_and_open(RecordingStore)

        drv = store.read_derivation(nanopynix_store.StorePath(f"{BOGUS}.drv"))

        assert drv["structured_attrs"] == '{"hardening":{"format":false}}'
        assert "__json" not in drv["env"]
        assert drv["outputs"]["out"]["path"] == f"/nix/store/{RECORDED_DRV_OUT}"
        assert drv["builder"] == "/bin/sh"
        # From `Derivation::nameFromPath`, not from anything the store said --
        # the ATerm carries no name of its own.
        assert drv["name"] == "bogus"

    def test_a_malformed_reply_fails_naming_the_path(self) -> None:
        """A store returning nonsense must produce Nix's own parse error.

        The alternative -- a cast failure, or a silently empty derivation -- is
        the class of defect this whole file exists for. The path has to appear
        because that is the only thing telling a caller *which* store object was
        unreadable.
        """
        if not _dispatch_is_available("read_derivation"):
            pytest.skip("this Nix cannot dispatch read_derivation")

        class ReturnsGarbage(nanopynix.StoreImpl):
            def read_derivation(self, path: str) -> str:
                return "not an ATerm"

        store = register_and_open(ReturnsGarbage)

        with pytest.raises(Exception, match=f"{BOGUS}.drv"):
            store.read_derivation(nanopynix_store.StorePath(f"{BOGUS}.drv"))

    def test_a_store_that_does_not_implement_it_still_cannot_serve_one(self) -> None:
        """The behaviour this addition exists to change, pinned as it was.

        Serving derivations was never possible through ``query_path_info``: a
        store that answers every other query, including reporting the path
        valid, still fails here. This is the measurement behind the claim in
        ``store_impl.py``, kept as a test so the docstring cannot quietly go
        stale -- and it holds on every supported version, since it depends on
        the accessor rather than on the virtual.
        """

        class AnswersEverythingButDerivations(nanopynix.StoreImpl):
            def is_valid_path_uncached(self, path: str) -> bool:
                return True

            def query_path_info(self, path: str) -> dict[str, Any]:
                return dict(FULL_INFO)

        store = register_and_open(AnswersEverythingButDerivations)
        path = nanopynix_store.StorePath(f"{BOGUS}.drv")

        assert store.is_valid_path(path) is True
        with pytest.raises(Exception, match=BOGUS):
            store.read_derivation(path)

    def test_a_store_that_does_not_implement_it_answers_as_its_underlying_one(
        self, store: Any, store_seeded_path: Any
    ) -> None:
        """A store that implements nothing must still serve derivations.

        This is the reachability claim, not a claim about the ``underlying->``
        line: deleting that line leaves this green, because ``getFSAccessor``
        delegates too, so ``nix::Store``'s own reader reaches the same bytes by
        a longer route. Measured by mutation rather than assumed, and recorded
        again in :data:`DELEGATION_INDISTINGUISHABLE_FROM_BASE`.

        What it does pin is the difference from
        :meth:`test_a_store_that_does_not_implement_it_still_cannot_serve_one`
        just above -- with an ``underlying_store`` a derivation is readable, and
        without one it is not -- and it compares messages rather than exception
        types, since on a path that is not a derivation both sides raise
        ``Error`` and only the message says which failure it was.
        """
        if not _dispatch_is_available("read_derivation"):
            pytest.skip("this Nix cannot dispatch read_derivation")

        class Delegating(nanopynix.StoreImpl):
            underlying_store = store

        delegating = register_and_open(Delegating)

        # `match` on both rather than only comparing the two: without it, a
        # future where each side independently reported the same unhelpful "not
        # a valid store path" would still read as agreement. Both must be the
        # *parse* failure, which is only reachable if the bytes were read.
        with pytest.raises(Exception, match="error parsing derivation") as delegated:
            delegating.read_derivation(store_seeded_path)
        with pytest.raises(Exception, match="error parsing derivation") as direct:
            store.read_derivation(store_seeded_path)

        assert str(delegated.value) == str(direct.value)


def _warnings_from_opening(python_store_cls: type[Any]) -> list[str]:
    """Nix *warnings* emitted while opening a store of ``python_store_cls``.

    ``nix::warn`` goes to ``nix::logger``, not to Python's ``warnings``
    module, so neither ``pytest.warns`` nor ``caplog`` sees it. Installing a
    collector for the duration of the open is the only way to observe it, and
    ``drain()`` is the synchronous side of that collector -- ``open_store`` is
    a blocking call with no event loop to stream from.

    ``PyLogger`` overrides ``Logger::warn`` outright rather than letting it
    fall through to ``log(lvlWarn, ...)``, so a warning arrives as its own
    ``"warn"`` action with the text at index 3, where an ordinary ``"msg"``
    carries a level first and the text at index 4. Selecting on the action is
    therefore part of the assertion, not plumbing: it distinguishes a warning
    from an info line that happens to mention the same method.
    """
    collector = LogCollector()
    nanopynix_util.install_logger(collector.callback)
    try:
        register_and_open(python_store_cls)
    finally:
        nanopynix_util.remove_logger()
    return [event[3] for event in collector.drain() if event[2] == "warn"]


class TestTheUndispatchableCaseIsSaidOutLoud:
    """What a build that cannot dispatch ``read_derivation`` does about it.

    ``Store::readDerivation`` is non-virtual before Nix 2.32, so there is no
    hook to install and a store's override is never called. The defect that
    would produce -- a method that exists, type-checks and is silently dead --
    is exactly what the rest of this file exists to prevent, so the 2.31 build
    warns at store-open time and ``build_info()`` carries the fact as a
    capability.

    The two tests below are each other's other half, and only one of them runs
    on any given build: a version marker rather than a runtime ``if``, so the
    reason a test did not run appears in the report rather than as a silent
    pass. Only the 2.31 job exercises the warning; a green 2.34 run says
    nothing about it, which is why the ``maximum`` test exists at all.
    """

    @pytest.mark.nix_version(maximum="2.32", reason="the warning only exists where dispatch does not")
    def test_a_build_without_dispatch_warns_when_a_store_implements_it(self) -> None:
        class ImplementsIt(nanopynix.StoreImpl):
            def read_derivation(self, path: str) -> str:
                return RECORDED_DRV_ATERM

        warnings = _warnings_from_opening(ImplementsIt)

        assert any("read_derivation" in text for text in warnings), warnings
        # The capability is the branchable form of the same fact, and a caller
        # told to consult it must find it false here.
        assert _dispatch_is_available("read_derivation") is False

    @pytest.mark.nix_version(minimum="2.32", reason="a dispatching build has nothing to warn about")
    def test_a_build_with_dispatch_says_nothing(self) -> None:
        """The inverse: a build that *can* dispatch must not warn about it.

        The guard being dropped -- warning on every version rather than only
        where dispatch is missing -- is the mistake only this half can catch,
        measured: with the ``#if`` replaced by ``#if 1``, the 2.31 test above
        stays green, because on 2.31 the two conditions are the same. A guard
        *inverted* rather than dropped fails both halves and needs neither.

        This half asserts an absence, so on its own it would also pass with a
        collector that captured nothing. What rules that out is that both
        halves call the same :func:`_warnings_from_opening`, and the 2.31 job
        asserts a *presence* through it. Neither version proves the pair
        alone; the matrix does. Stated because it was nearly missed: an
        earlier draft of the helper read the wrong tuple index and this test
        passed anyway, while the 2.31 one failed and said so.
        """

        class ImplementsIt(nanopynix.StoreImpl):
            def read_derivation(self, path: str) -> str:
                return RECORDED_DRV_ATERM

        warnings = _warnings_from_opening(ImplementsIt)

        assert not [text for text in warnings if "read_derivation" in text], warnings
        assert _dispatch_is_available("read_derivation") is True


def outcome(call: Callable[[], Any]) -> tuple[str, Any]:
    """What ``call`` did, as a comparable value -- a result or a failure kind."""
    try:
        return ("returned", call())
    except Exception as exc:
        return ("raised", type(exc).__name__)


# The same operations again, but expressed against a store that really holds the
# path -- so the underlying store gives a substantive answer rather than every
# call failing identically on both sides, which would make the comparison below
# vacuous.
DELEGATION_CHECKS: dict[str, Callable[[Any, Any], Any]] = {
    "is_valid_path_uncached": lambda s, p: s.is_valid_path(p),
    "query_path_info": lambda s, p: s.query_path_info(p)["nar_size"],
    "query_path_from_hash_part": lambda s, p: str(
        s.query_path_from_hash_part(p.to_string().split("-")[0])
    ),
    "query_referrers": lambda s, p: s.query_referrers(p),
    "query_valid_derivers": lambda s, p: s.query_valid_derivers(p),
    "query_derivation_outputs": lambda s, p: s.query_derivation_outputs(p),
    "query_all_valid_paths": lambda s, p: p.to_string()
    in [q.split("/")[-1] for q in s.query_all_valid_paths()],
    "query_substitutable_paths": lambda s, p: s.query_substitutable_paths([p]),
    "add_temp_root": lambda s, p: s.add_temp_root(p),
    "ensure_path": lambda s, p: s.ensure_path(p),
    # The only one that does not take the path -- it verifies the whole store.
    "verify_store": lambda s, _p: s.verify_store(False, False),
    "compute_fs_closure": lambda s, p: sorted(s.compute_fs_closure(p, False, False, False)),
    "query_missing": lambda s, p: s.query_missing([f"/nix/store/{p.to_string()}"]),
    # Deliberately the seeded path, which is valid but not a derivation: both
    # sides then fail in Nix's parser having actually read the bytes, which is
    # the reachability this asserts. A nonexistent `.drv` would raise
    # `InvalidPath` on both without either side reading anything.
    #
    # It does not distinguish the `underlying->` line -- measured, see
    # `DELEGATION_INDISTINGUISHABLE_FROM_BASE` below for why not.
    "read_derivation": lambda s, p: s.read_derivation(p),
}

# `optimise_store` is the one operation left out, and on purpose: delegating it
# would hard-link the real test store's contents. The cost of proving that one
# branch is a mutated store shared with every other test in the session.
DELEGATION_UNCHECKED = {"optimise_store"}

# What this test does *not* pin, measured rather than guessed by running the
# same checks against a store with no `underlying_store` at all.
#
# For the first four, `nix::Store`'s own answer already matches a real store's
# for a freshly added path: nothing derives it, nothing substitutes it, nothing
# roots it, and there is nothing to verify. So deleting their `underlying->` line
# outright would not change the result, and this test would stay green.
#
# `read_derivation` is vacuous for a different and more interesting reason,
# measured the same way: deleting `underlying->readDerivation` changes nothing
# observable, because `getFSAccessor` delegates as well, so `nix::Store`'s own
# reader reaches the same bytes by a longer route. The line stays -- every other
# override here keeps its delegation and going through the accessor is the
# indirect path -- but no test can distinguish it, and claiming otherwise would
# be worse than saying so.
#
# It does still catch the more likely mistake, a delegation pointed at the wrong
# method -- verified by mutation: swapping `underlying->queryValidDerivers` for
# `underlying->queryValidPaths`, which has the same signature and compiles
# cleanly, fails here and names `query_valid_derivers` alone. Making the others
# non-vacuous would take a built derivation, a substituter and a GC root, which
# is a different and much heavier fixture than this file's.
DELEGATION_INDISTINGUISHABLE_FROM_BASE = {
    "query_valid_derivers",
    "query_substitutable_paths",
    "add_temp_root",
    "verify_store",
    "read_derivation",
}


class TestEveryOperationDelegates:
    """A store that implements nothing must be indistinguishable from its underlying one.

    Each dispatch site is three branches -- Python, then ``underlying``, then
    ``nix::Store`` -- and the other tests here pin the first and the third.
    Without this the middle one is unverified for every operation except
    ``query_all_valid_paths``, so transposing two ``underlying->`` calls (say
    ``queryValidDerivers`` for ``queryValidPaths``, which have the same
    signature) would compile, run, and go unnoticed.

    See :data:`DELEGATION_INDISTINGUISHABLE_FROM_BASE` for the five operations
    this cannot fully pin, and why.
    """

    def test_the_delegation_table_covers_the_interface(self) -> None:
        assert set(DELEGATION_CHECKS) | DELEGATION_UNCHECKED == set(
            nanopynix.DISPATCHABLE_METHODS
        )

    @pytest.mark.parametrize("name", sorted(DELEGATION_CHECKS))
    def test_the_answer_is_the_underlying_store_s_answer(
        self, name: str, store: Any, store_seeded_path: Any
    ) -> None:
        """Compared against the real store rather than a fixed expectation.

        What each operation returns for a seeded path is not the point and
        varies by backend; that the two agree is. Failures are compared by
        exception type for the same reason -- an operation that raises on both
        is still delegating, whereas one that raises only when wrapped is not.
        """
        if not _dispatch_is_available(name):
            pytest.skip(f"this Nix cannot dispatch {name}, so there is no delegation branch")

        class Delegating(nanopynix.StoreImpl):
            underlying_store = store

        delegating = register_and_open(Delegating)
        check = DELEGATION_CHECKS[name]

        assert outcome(lambda: check(delegating, store_seeded_path)) == outcome(
            lambda: check(store, store_seeded_path)
        )
