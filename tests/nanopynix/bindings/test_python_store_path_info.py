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


def open_python_store(query_path_info: Callable[[str], Any]) -> Any:
    """Register a one-off Python store whose query_path_info is ``query_path_info``."""
    scheme = f"pytest-pystore-{next(_scheme_counter)}"

    class PythonStore:
        def is_valid_path_uncached(self, path: str) -> bool:
            return True

        def query_path_info(self, path: str) -> Any:
            return query_path_info(path)

        def query_path_from_hash_part(self, hash_part: str) -> None:
            return None

    class Factory:
        @staticmethod
        def open_store() -> object:
            return PythonStore()

    nanopynix_store.register_store_implementation(scheme, scheme, [scheme], Factory())
    return nanopynix.open_store(f"{scheme}://example")


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
    scheme = f"pytest-pystore-hash-{next(_scheme_counter)}"

    class PythonStore:
        def is_valid_path_uncached(self, path: str) -> bool:
            return True

        def query_path_info(self, path: str) -> None:
            return None

        def query_path_from_hash_part(self, hash_part: str) -> str:
            return returned

    class Factory:
        @staticmethod
        def open_store() -> object:
            return PythonStore()

    nanopynix_store.register_store_implementation(scheme, scheme, [scheme], Factory())
    store = nanopynix.open_store(f"{scheme}://example")

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

    @staticmethod
    def _open_delegating_store(real: Any, **overrides: Any) -> Any:
        scheme = f"pytest-pystore-under-{next(_scheme_counter)}"

        class PythonStore:
            underlying_store = real

        for attribute, value in overrides.items():
            setattr(PythonStore, attribute, value)

        class Factory:
            @staticmethod
            def open_store() -> object:
                return PythonStore()

        nanopynix_store.register_store_implementation(scheme, scheme, [scheme], Factory())
        return nanopynix.open_store(f"{scheme}://example")

    def test_opening_a_store_with_an_underlying_store_works_at_all(self, store: Any) -> None:
        """The whole feature used to die here, before any method was called."""
        delegating = self._open_delegating_store(store)

        assert isinstance(delegating, nanopynix_store.Store)

    def test_unimplemented_methods_reach_the_underlying_store(
        self, store: Any, store_seeded_path: Any
    ) -> None:
        """A store that implements nothing still answers, via the real one.

        Both assertions matter: ``is_valid_path`` would be ``False`` and
        ``query_path_info`` would raise "is not valid" if the fallthrough were
        skipped rather than taken, so neither can pass by accident.
        """
        delegating = self._open_delegating_store(store)

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

        def always_invalid(_self: Any, _path: Any) -> bool:
            return False

        delegating = self._open_delegating_store(store, is_valid_path_uncached=always_invalid)

        assert delegating.is_valid_path(store_seeded_path) is False
