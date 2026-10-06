"""pynix loads the plugins that ``plugin-files`` names, before a store opens.

Nix loads plugins once per process, so each case runs in a child. A
``plugin-files`` entry that names no file makes Nix fail as it loads it,
which shows that the load happened without a real plugin. huggorm's
``test_plugins.py`` proves that a real one, ``nix-tcp-store``, works.
"""

from __future__ import annotations

import re
import sys

from test_support.subprocess_output import run_process

_CHILD = """
import anyio
from pynix import parse

anyio.run(parse(["store", "info", "--store", "dummy://"]).run)
"""

_MISSING = "/nonexistent/pynix-test-plugin.so"


async def _store_info(nix_config: str) -> tuple[int, str]:
    result = await run_process(["env", f"NIX_CONFIG={nix_config}", sys.executable, "-c", _CHILD])
    return result.returncode, re.sub(r"\s+", "", result.stderr)


async def test_a_plugin_that_plugin_files_names_is_loaded() -> None:
    returncode, stderr = await _store_info(f"plugin-files = {_MISSING}")

    assert returncode != 0
    assert _MISSING in stderr, stderr


async def test_no_plugin_files_loads_nothing() -> None:
    returncode, stderr = await _store_info("")

    assert returncode == 0, stderr
