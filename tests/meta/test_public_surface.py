"""What ``nanopynix`` and ``nanopynix.exceptions`` say they export.

``__all__`` is a list a person maintains, and the tree it describes is not.
Measured before this file existed: 19 names that ``nanopynix/__init__.py``
imports on purpose were absent from its ``__all__``, and 5 exception classes
defined in ``nanopynix/exceptions.py`` were absent from that module's. Each
one is still importable, so nothing broke and nothing noticed -- but
``from nanopynix import *`` missed them, and so does every tool that reads
``__all__`` as the public surface.

Each test below derives its set from the module object, so none of them needs
a ledger. That is deliberate, and it is the difference from
``test_consumer_surface.py``: whether a *consumer* may reach into a private
module is a judgement, and whether a name the package already binds should
appear in the list it publishes is not.

**What this file does not check.** Whether a name deserves to be public. That
is the judgement ``tests/AGENTS.md`` names as out of reach for a meta test.
Adding an export to make a test pass is the wrong direction here: the question
these tests ask is only whether the list matches what the module binds.
"""

from __future__ import annotations

import inspect
from types import ModuleType

import nanopynix
from nanopynix import exceptions

# `from __future__ import annotations` leaves this binding behind on every
# module that uses it. It is a `__future__._Feature`, not an export, and it is
# the one public name that no `__all__` in this repository should carry.
_FUTURE_FLAG = "annotations"

_MINIMUM_EXPORTS = 100
"""Sanity floor for the derived set, so a broken filter cannot pass silently.

The package exported about 140 names when this file landed. The number is a
guard, not a target -- see the guard test."""


def _bound_public_names(module: ModuleType) -> set[str]:
    """Public names a module binds, less its submodules and the future flag.

    A submodule is bound as a side effect of ``from nanopynix.x import y``, so
    it is not evidence of intent either way. A submodule the package does mean
    to export is listed in ``__all__`` like any other name, and the other
    direction of this test covers it.
    """
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and name != _FUTURE_FLAG and not isinstance(value, ModuleType)
    }


def _exception_classes(module: ModuleType) -> set[str]:
    """Exception classes the module defines itself, not ones it imports."""
    return {
        name
        for name, value in vars(module).items()
        if inspect.isclass(value) and issubclass(value, BaseException) and value.__module__ == module.__name__
    }


def test_the_derived_sets_are_not_empty() -> None:
    """Fail loudly if the filters above stop matching anything.

    Both tests below compare a derived set against ``__all__``, and a filter
    that matched nothing would make each of them pass by checking nothing.
    That silent no-op is what every test in this directory guards against.
    """
    bound = _bound_public_names(nanopynix)
    assert len(bound) >= _MINIMUM_EXPORTS, f"only {len(bound)} public names on nanopynix; the filter is wrong"
    assert "NixError" in bound, "a known export is missing from the derived set; the filter is wrong"
    assert len(_exception_classes(exceptions)) >= _MINIMUM_EXPORTS // 4


def test_the_future_flag_is_not_an_export() -> None:
    """``annotations`` is the artefact the filter above exists to remove.

    Pinned, because the filter is one name in a set comprehension and a
    reader has no other way to see why it is there.
    """
    assert _FUTURE_FLAG in vars(nanopynix), "the package no longer uses future annotations; delete the filter"
    assert _FUTURE_FLAG not in nanopynix.__all__
    assert _FUTURE_FLAG not in _bound_public_names(nanopynix)


def test_every_public_name_the_package_binds_is_exported() -> None:
    missing = sorted(_bound_public_names(nanopynix) - set(nanopynix.__all__))
    assert not missing, (
        f"{len(missing)} public name(s) are bound on the nanopynix package but absent from "
        "__all__. Add each one to __all__, or stop importing it at the top level if it is not "
        f"meant to be public: {missing}"
    )


def test_every_exported_name_exists() -> None:
    """The other direction: a typo in ``__all__`` breaks ``import *`` alone."""
    absent = sorted(name for name in nanopynix.__all__ if not hasattr(nanopynix, name))
    assert not absent, f"nanopynix.__all__ names {len(absent)} thing(s) the package does not bind: {absent}"


def test_every_exception_class_is_in_the_exceptions_all() -> None:
    missing = sorted(_exception_classes(exceptions) - set(exceptions.__all__))
    assert not missing, (
        f"{len(missing)} exception class(es) are defined in nanopynix/exceptions.py but absent "
        f"from its __all__: {missing}"
    )


def test_every_exception_class_reaches_the_top_level() -> None:
    """A caller needs the name to write ``except``, and reaches for it there.

    Only the classes. ``nanopynix.exceptions`` also exports the resolvers that
    build them from the wire, and those stay one import deeper: they are how
    an engine turns a response into an exception, not something a caller of
    the library calls.
    """
    classes = _exception_classes(exceptions) & set(exceptions.__all__)
    missing = sorted(classes - set(nanopynix.__all__))
    assert not missing, (
        f"{len(missing)} exception class(es) are public in nanopynix.exceptions but not exported "
        f"by the nanopynix package, so a caller cannot catch them by name: {missing}"
    )
