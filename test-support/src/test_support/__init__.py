"""Test helpers that know nothing about Nix, shared by every project here.

**A module belongs here when more than one project's suite needs it and it
names no Nix concept.** That rule is what issue #130 measured. Before the
split, every one of these lived under ``tests/support/``, which imports as
``tests.support.<name>`` and therefore resolves from the repository rootdir
alone. A subproject with its own ``pytest.ini`` -- ``grpclib-transports`` and
``pytest-agent`` -- could reach none of it, and one of them had already written
its own copy of a sandbox check.

The measurement, over the test trees that existed at the time:

===================== ===============================================
module                trees that import it
===================== ===============================================
``subprocess_output`` the root conftest, gates, harness, nanopynix, pynix
``notes``             meta, nanopynix
``hang_report``       the root conftest, harness
``git_fixtures``      nanopynix, pynix
===================== ===============================================

**The Nix-aware helpers are deliberately not here.** ``nix_environment`` alone
is imported 36 times by nanopynix's suite and 18 times by pynix's, and it
builds a real store; it lives in ``nanopynix-testing`` beside the library whose
concepts it names. A helper here must stay useful to a project that never
loads Nix at all.
"""

from __future__ import annotations
