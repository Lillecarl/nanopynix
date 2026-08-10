"""Runtime mirror of `typing.TYPE_CHECKING`, flipped only for beartype coverage.

`typing.TYPE_CHECKING` is always `False` at runtime, so `if TYPE_CHECKING:`
guards keep type-only imports (used purely to avoid circular imports or trim
the runtime dependency graph) out of the real import graph -- pyright treats
`TYPE_CHECKING` as `True` when analyzing, Python treats it as `False` when
running.

Modules that use `if TYPE_CHECKING or BEARTYPING:` instead ask for that same
name to *also* become a real runtime import when this flag is set, which
`tests/support/beartype_hook.py` does (via the `NANOPYNIX_BEARTYPING`
environment variable, read below) before anything else is imported --
because beartype's runtime checks need a real, importable object to check
against, not a string. Flipping `typing.TYPE_CHECKING` itself for this would
affect *every* module process-wide, including third-party dependencies that
rely on it staying `False` to avoid their own circular imports (confirmed:
this broke `betterproto2`). This flag is scoped to modules that explicitly
opt in by naming it, so it can never affect code that doesn't check it.

An environment variable rather than a settable Python attribute: this module
lives inside the `nanopynix` package, so reaching it via
`nanopynix._typechecking` from outside first requires `nanopynix/__init__.py`
itself to finish running -- by which point its own cascade of submodule
imports (the very ones that need to see this flag) has already completed.
There is no way to import a submodule of `nanopynix` without first fully
executing `nanopynix/__init__.py`. An environment variable has no such
ordering dependency: it is process state, not a Python object reachable only
through the very import chain it needs to influence.

Stays unset (`False`) in every normal (non-test) run, so production import
behavior is unaffected. Some `if TYPE_CHECKING or BEARTYPING:` imports may
still turn out to hit a genuine nanopynix-internal circular import once
actually imported -- that's a real constraint the guard existed to avoid, not
a bug in this flag, and is handled case by case rather than assumed away.

This module is deliberately dependency-free and is imported by name from
`pynix` and `nanopynix_helpers` as well as from `nanopynix` itself. It is
shared test-instrumentation plumbing rather than library API, so it stays
private (a public re-export would be surface with no caller outside this
repository); the usual "consumers depend on public nanopynix APIs" rule is
waived here rather than silently broken.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

BEARTYPING: bool = os.environ.get("NANOPYNIX_BEARTYPING") == "1"


def no_runtime_type_check[F: Callable[..., Any]](func: F) -> F:
    """Exempt ``func`` from beartype's runtime checks, keeping static ones.

    `typing.no_type_check` also achieves the runtime exemption -- beartype
    honors the `__no_type_check__` attribute it sets -- but it is a *typing*
    construct, and type checkers act on it: pyright erases the signature
    (every parameter becomes `Unknown`) and stops reporting errors in the
    body. Measured, not assumed. Spending that on functions like
    `Session.__init__`, whose signature is the most important one in the
    library, is a bad trade for a runtime-only concern.

    This decorator sets the same attribute without the typing meaning.
    Pyright sees an identity function -- annotations intact, body still
    checked -- while beartype's decorator returns early exactly as it does
    for `typing.no_type_check`. That early return happens *before* it
    analyzes any hint, so this also suppresses failures that occur at
    decoration time rather than at call time (an unsupported hint such as
    `Never`, or a forward reference beartype cannot resolve), not just
    parameter checks.

    Reach for it when a runtime check would be wrong, not merely noisy:

    * the function validates its own inputs and documents a specific
      exception type that beartype's check would preempt (beartype's
      violations subclass neither `TypeError` nor `ValueError`);
    * the annotation describes a duck-typed boundary, where the real
      argument satisfies the interface without being an instance of the
      annotated type;
    * beartype cannot evaluate the hint at all, and the alternative is
      weakening the annotation itself.
    """
    # `__no_type_check__` is the attribute `typing.no_type_check` sets and the
    # one `beartype` looks for; setting it directly is the whole mechanism.
    func.__no_type_check__ = True  # type: ignore[attr-defined] -- functions accept arbitrary attributes; typeshed does not declare this one
    return func
