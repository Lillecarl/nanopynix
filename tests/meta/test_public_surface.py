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

import ast
import importlib
import inspect
from pathlib import Path
from types import ModuleType

import nanopynix
from nanopynix import exceptions, protocols

# `from __future__ import annotations` leaves this binding behind on every
# module that uses it. It is a `__future__._Feature`, not an export, and it is
# the one public name that no `__all__` in this repository should carry.
_FUTURE_FLAG = "annotations"

_MINIMUM_EXPORTS = 100
"""Sanity floor for the derived set, so a broken filter cannot pass silently.

The package exported about 140 names when this file landed. The number is a
guard, not a target -- see the guard test."""


def _bound_public_names(module: ModuleType) -> set[str]:
    """Public names a module offers, less its submodules and the future flag.

    A submodule is bound as a side effect of ``from nanopynix.x import y``, so
    it is not evidence of intent either way. A submodule the package does mean
    to export is listed in ``__all__`` like any other name, and the other
    direction of this test covers it.

    ``vars()`` is no longer the whole answer for the package. Issue #123 made
    every public name lazy, so ``nanopynix/__init__.py`` binds almost nothing
    until something reads a name. ``_NAME_TO_MODULE`` is where the intent
    lives now, and a private table is what a meta test is allowed to read.
    """
    lazy: set[str] = set(getattr(module, "_NAME_TO_MODULE", {}))
    bound = {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and name != _FUTURE_FLAG and not isinstance(value, ModuleType)
    }
    return lazy | bound


def _type_checking_imports(module: ModuleType) -> dict[str, str]:
    """Name to origin, read from the ``if TYPE_CHECKING:`` blocks of a module.

    The lazy surface is written twice, and this reads the half that runs
    nowhere. pyright cannot follow ``_NAME_TO_MODULE``, so the block is what
    gives a caller of ``from nanopynix import NixError`` a type; the table is
    what gives the caller a value. A name in one and not the other fails in a
    way that no run of the suite reports.
    """
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if test not in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.ImportFrom) and statement.module:
                for alias in statement.names:
                    found[alias.asname or alias.name] = statement.module
    return found


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


def test_every_protocol_reaches_the_top_level() -> None:
    """A protocol a caller cannot reach is a protocol a caller does not use.

    ``AsyncSession`` was absent here when this test landed, one commit after
    the protocol itself. Six siblings were re-exported and the seventh was
    not, so the omission looked like nothing.

    The cost is not a missing import. A protocol is the name that engine-neutral
    code writes in its annotations, and a caller who cannot reach it annotates
    the concrete engine class instead -- which pins that code to one engine and
    is exactly what this repository built the protocols to avoid.
    """
    missing = sorted(set(protocols.__all__) - set(nanopynix.__all__))
    assert not missing, (
        f"{len(missing)} protocol(s) are public in nanopynix.protocols but not exported by the "
        f"nanopynix package, so engine-neutral code cannot name them: {missing}"
    )


def test_the_lazy_table_and_the_type_checking_block_agree() -> None:
    """The two halves of the lazy surface name the same things, from the same modules.

    ``__getattr__`` reads ``_NAME_TO_MODULE`` and pyright reads the
    ``if TYPE_CHECKING:`` block above it. A name added to one alone still
    imports at run time and still type-checks -- one of the two halves simply
    stops covering it, silently. This is the check that the mechanism itself
    needs, and it is new with the mechanism.

    Submodules are excluded on both sides. ``inproc``, ``rpc`` and ``stores``
    are in the block and in ``_LAZY_SUBMODULES``, and not in the table.
    """
    table: dict[str, str] = dict(nanopynix._NAME_TO_MODULE)
    submodules: frozenset[str] = nanopynix._LAZY_SUBMODULES
    declared = {name: origin for name, origin in _type_checking_imports(nanopynix).items() if name not in submodules}

    assert declared, "no TYPE_CHECKING imports found in nanopynix/__init__.py; the parser is wrong"

    missing = sorted(set(table) - set(declared))
    assert not missing, (
        f"{len(missing)} name(s) resolve through _NAME_TO_MODULE but no TYPE_CHECKING import "
        f"declares them, so pyright cannot type them: {missing}"
    )

    extra = sorted(set(declared) - set(table))
    assert not extra, (
        f"{len(extra)} name(s) are imported for the type checker but absent from "
        f"_NAME_TO_MODULE, so reading them at run time raises AttributeError: {extra}"
    )

    disagree = sorted(name for name, origin in declared.items() if table[name] != origin)
    assert not disagree, (
        f"{len(disagree)} name(s) come from a different module in the two halves, so the type "
        f"and the value can be different things: {disagree}"
    )


def test_the_impl_table_names_every_module_that_is_there() -> None:
    """``pynix._impl`` is written three times, and two of them are lists.

    The package holds ``_SUBMODULES``, an ``if TYPE_CHECKING:`` import block,
    and the files themselves. A module missing from the frozenset raises
    ``AttributeError`` the first time the command that needs it runs, and no
    import-time check sees that: leaving ``main`` out cost every ``pynix-lsp``
    end-to-end test a ``Server process exited with return code: 1``, and every
    other suite stayed green.

    The files on disk are the truth here, so this derives from them.
    """
    package = importlib.import_module("pynix._impl")
    directory = Path(str(package.__file__)).parent
    on_disk = {path.stem for path in directory.glob("*.py") if path.stem != "__init__"}

    assert on_disk, "no implementation modules found; the glob is wrong"

    declared: frozenset[str] = package._SUBMODULES  # noqa: SLF001 -- a meta test reads the mechanism it guards
    assert declared == on_disk, (
        "pynix/_impl/_SUBMODULES and the files in that directory disagree. "
        f"only on disk: {sorted(on_disk - declared)}; only in the table: {sorted(declared - on_disk)}"
    )

    typed = set(_type_checking_imports(package))
    assert typed == on_disk, (
        "the TYPE_CHECKING block of pynix/_impl and the files in that directory disagree, so "
        f"pyright cannot type one of them. only on disk: {sorted(on_disk - typed)}; "
        f"only in the block: {sorted(typed - on_disk)}"
    )
