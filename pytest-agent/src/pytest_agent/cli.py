from __future__ import annotations

import sys

import pytest

from pytest_agent._entry_points import PLUGIN_MODULE, plugin_registered_via_entry_points


def main() -> None:
    """Console-script entry point: `pytest-agent [args...]` is `pytest --agent [args...]`.

    Runs in-process via pytest's own public `pytest.main()` API rather than
    spawning a subprocess. When this package is properly installed (a real
    `pip install`/nix package), its own pytest11 entry point already makes
    pytest load the plugin, and passing it again via `plugins=` crashes:
    pluggy registers it once under the entry point's declared name ("agent")
    and refuses a second registration of the same module object under the
    different name pytest.main()'s `plugins=` list would use. So the plugin
    is only passed explicitly as a fallback, for when pytest-agent is merely
    on PYTHONPATH with no installed distribution metadata at all (as in its
    own dev/test environment) and entry-point discovery has nothing to find.
    """
    extra_plugins: list[str] = [] if plugin_registered_via_entry_points() else [PLUGIN_MODULE]
    raise SystemExit(pytest.main(["--agent", *sys.argv[1:]], plugins=extra_plugins))
