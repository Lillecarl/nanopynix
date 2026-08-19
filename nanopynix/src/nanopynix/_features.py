"""The experimental features that nanopynix turns on for every store.

**This tuple sits alone in its own module so that ``init_libstore`` is cheap
to reach.** It used to live in ``nanopynix.settings``, which builds a pydantic
model for each of the four Nix setting surfaces and costs about 400 ms to
import. A consumer that reads one name -- and issue #123 names a real one, the
planner of ``ddrn/examples/venv-graph`` -- paid all of that for a tuple of five
strings.

``nanopynix.settings`` re-exports the name, so
``nanopynix.settings.DEFAULT_EXPERIMENTAL_FEATURES`` still resolves.
"""

from __future__ import annotations

DEFAULT_EXPERIMENTAL_FEATURES = (
    "flakes",
    "nix-command",
    "ca-derivations",
    "dynamic-derivations",
    "recursive-nix",
)
