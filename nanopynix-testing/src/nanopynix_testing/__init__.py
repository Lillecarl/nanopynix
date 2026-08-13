"""Test fixtures and pytest markers that name a concept of Nix.

**A module belongs here when it names a store, an evaluator, a linked Nix
version or a worker process.** That is the one rule that separates this project
from ``test-support``, which holds the helpers that name none of those and must
stay useful to a project that never loads Nix.

Issue #130 measured the split. ``nix_environment`` alone answers 35 imports in
nanopynix's suite and 14 in pynix's, and it builds a real store, so no project
that avoids Nix can carry it. The five modules here:

===================== ==========================================================
module                what it gives a suite
===================== ==========================================================
``nix_environment``   hermetic local-store and native-daemon fixtures
``nix_runtime``       the facts of the linked Nix build, and the marker plugin
``nix_markers``       the version and capability markers, as functions
``soak``              the concurrent re-run of the suite, for ThreadSanitizer
``worker_death``      the helpers that kill an RPC worker and read the result
===================== ==========================================================

**These modules were a private tree until issue #130.** They imported as
``tests.support.<name>``, so only a run whose rootdir is the repository could
reach them, and a suite that moves into its own project could not. This project
is what makes that move possible.
"""

from __future__ import annotations
