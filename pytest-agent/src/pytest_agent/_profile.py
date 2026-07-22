"""An opt-in ``profile`` fixture: wraps a test in a pyinstrument profiler and
writes a text report to disk, no test-body changes needed beyond requesting
the fixture.

``pyinstrument`` is a hard dependency of pytest-agent itself (not an
optional extra): pytest-agent is the thing that's optional -- only pulled
into a project's environment (here, nix/shell.nix) when its detail-on-disk
philosophy is wanted at all -- so there's no lighter-weight "pytest-agent
without a profiler" install worth supporting.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pyinstrument import Profiler

from pytest_agent._capture import nodeid_to_relpath
from pytest_agent._runtime import RUNTIME_PLUGIN_NAME

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_agent._runtime import AgentRuntime


def _report_dir(request: pytest.FixtureRequest) -> Path:
    """Where to write this test's profile report.

    Alongside the test's own ``.log``/``.json`` (the current run's
    directory) when agent mode is active -- otherwise a fixed, always-
    overwritten ``profiles/`` directory under ``--agent-dir``, since a plain
    (non-agent-mode) run has no per-run directory to be "alongside" at all.
    """
    runtime = cast("AgentRuntime | None", request.config.pluginmanager.get_plugin(RUNTIME_PLUGIN_NAME))
    if runtime is not None:
        return runtime.root
    agent_dir = cast("str", request.config.getoption("agent_dir"))
    root = Path(agent_dir)
    if not root.is_absolute():
        root = request.config.rootpath / root
    return root / "profiles"


@pytest.fixture
def profile(request: pytest.FixtureRequest) -> Generator[Profiler]:
    """Profile this test with pyinstrument, writing a text report next to its other pytest-agent output.

    Usage: add ``profile`` as a test parameter -- no other change needed.

    ```python
    def test_something_slow(profile):
        do_the_slow_thing()
    ```

    The yielded :class:`pyinstrument.Profiler` is rarely needed directly
    (profiling starts/stops automatically around the test), but is exposed
    in case a test wants to add a custom tag or inspect the session.
    """
    profiler = Profiler()
    profiler.start()
    try:
        yield profiler
    finally:
        profiler.stop()
        nodeid = request.node.nodeid  # type: ignore[reportUnknownMemberType] -- pytest's own stubs type Node.nodeid as Any
        relpath = nodeid_to_relpath(cast("str", nodeid))
        report_path = _report_dir(request) / relpath.parent / f"{relpath.name}.profile.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(profiler.output_text(unicode=True, color=False), encoding="utf-8")
