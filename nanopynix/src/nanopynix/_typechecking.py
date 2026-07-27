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
"""

from __future__ import annotations

import os

BEARTYPING: bool = os.environ.get("NANOPYNIX_BEARTYPING") == "1"
