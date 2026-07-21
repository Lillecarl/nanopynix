from __future__ import annotations

from importlib import metadata

PLUGIN_MODULE = "pytest_agent.plugin"


def plugin_registered_via_entry_points() -> bool:
    """Whether some installed distribution's pytest11 entry point already
    points at pytest_agent.plugin.

    True for a real pip/nix install (pytest-agent's own pyproject.toml
    declares this entry point). False when pytest-agent is merely on
    PYTHONPATH with no install metadata at all -- e.g. its own dev/test
    environment before it's packaged, where nothing else will load the
    plugin unless something passes `-p pytest_agent.plugin` explicitly.
    """
    return any(ep.value == PLUGIN_MODULE for ep in metadata.entry_points(group="pytest11"))
