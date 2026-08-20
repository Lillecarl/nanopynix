"""The one decorator this package needs from the beartype plumbing.

``nanopynix._typechecking`` holds the same five lines, and this is a second
copy on purpose. That module is inside the ``nanopynix`` package, so importing
it runs ``nanopynix/__init__.py`` and everything that package loads, and this
project declares no dependency on ``nanopynix`` at all -- see the note above
``dependencies`` in ``pyproject.toml``. Restating two statements is the cheaper
of the two costs.

``nanopynix/tests/test_beartype_instrumentation.py`` states what the attribute
does, and that suite covers both copies: the attribute is beartype's, not this
repository's, so a change of meaning would reach the other copy first.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def no_runtime_type_check[F: Callable[..., Any]](func: F) -> F:
    """Exempt ``func`` from beartype's runtime checks, keeping the static ones.

    ``typing.no_type_check`` sets the same attribute and also carries a typing
    meaning: pyright erases the signature and stops reading the body. This
    decorator is an identity function to a type checker, so the annotations
    and the body stay checked.

    Every declaration helper here needs it. ``file_option()`` says it returns
    ``str | None``, because that is the type of the attribute the declaration
    becomes, and it returns a :class:`~libpynix.command.Spec`. beartype would
    report each call as a violation of an annotation that is doing its job.
    """
    # `__no_type_check__` is the attribute `typing.no_type_check` sets and the
    # one `beartype` looks for; setting it directly is the whole mechanism.
    func.__no_type_check__ = True  # type: ignore[attr-defined] -- functions accept arbitrary attributes; typeshed does not declare this one
    return func
