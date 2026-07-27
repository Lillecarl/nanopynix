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

Two further groups here are about the shape of the interface rather than that
first set of bugs. ``TestStoreImplIsRequired`` covers the move from ``hasattr``
probing to ``nanopynix.StoreImpl`` override detection, and
``TestQueriesNoLongerAnswerForThePythonStore`` covers three queries that used to
invent an answer instead of asking the store or deferring to Nix.

Registration is process-global and cannot be undone, so each store here gets a
unique scheme; otherwise re-registering under a parametrized run would raise.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_bindings import store as nanopynix_store

import nanopynix

if TYPE_CHECKING:
    from collections.abc import Callable

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
