from __future__ import annotations

from pytest_agent._entry_points import plugin_registered_via_entry_points

pytest_plugins = ["pytester"]


def agent_plugin_cli_args() -> list[str]:
    """Extra argv needed for a subprocess pytest run to load pytest_agent.plugin.

    Empty when this process's own environment already provides it via a
    pytest11 entry point (a real install -- the subprocess inherits the same
    site-packages/PYTHONPATH), since passing `-p pytest_agent.plugin` on top
    of an already-active entry point crashes (pluggy refuses to register the
    same plugin module under two different names). Otherwise
    `-p pytest_agent.plugin`, needed when pytest-agent is merely on
    PYTHONPATH with no install metadata at all.
    """
    return [] if plugin_registered_via_entry_points() else ["-p", "pytest_agent.plugin"]
