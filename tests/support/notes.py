"""``pytest_agent.note``, and a no-op stand-in when the plugin is absent.

The packaged test runner (``nanopynix/tests.nix``) leaves pytest-agent out on
purpose: it auto-activates on import, and that runner is what CI executes. So
a module-level ``from pytest_agent import note`` fails at *collection* in CI,
on every Nix version and both backends, while every dev shell passes. It did.
CI stayed red for a day, and the failure named the collection error rather
than any subject the tests are about.

``tests/conftest.py`` already does this for the ``agent_notes`` fixture, and
its docstring records the first time this happened. A conftest can replace a
fixture. It cannot replace a module-level import, which is why this module
exists. Import ``note`` from here, never from ``pytest_agent`` directly --
``tests/test_agent_note_imports.py`` is the gate that keeps that true.
"""

from __future__ import annotations

import importlib.util

if importlib.util.find_spec("pytest_agent") is not None:
    from pytest_agent import note as note
else:

    def note(**values: object) -> None:
        """Discard the recording. A note is observability, never an assertion."""
