"""Do the init entry points enable the default features before a store can exist?

``ca-derivations`` is one of nanopynix's defaults, and Nix treats it
asymmetrically: ``LocalStore`` prepares its realisation SQL statements only if
the feature is on when the store is *constructed* (``local-store.cc:356``),
while ``queryRealisationUncached`` re-tests the flag at *query* time and
dereferences those statements (``:1563``). Enabling it after a store already
exists therefore trips ``assert(stmt.stmt)`` and takes the process down with
SIGABRT -- no exception, nothing to catch, no traceback.

``nanopynix.init_nix`` and ``nanopynix.init_libstore`` close that window by
enabling the defaults themselves, so every store nanopynix can open is built
with them already in force.

These run in subprocesses on purpose. The pytest process enables the same
features in a session-scoped autouse fixture before anything opens a store, so
in-process the window is already shut and the assertion could not fail here
however the library behaved.
"""

from __future__ import annotations

import sys

import pytest

from nanopynix.settings import DEFAULT_EXPERIMENTAL_FEATURES
from tests.support.subprocess_output import CompletedProcess, run_process

# Report the features Nix actually has on, straight after the entry point under
# test -- before this module's fix, only what nix.conf happened to supply.
_REPORT_SCRIPT = """
import nanopynix
nanopynix.{entry}(load_config=False)
print(nanopynix.get_setting("experimental-features"))
"""

# The abort itself: build a store while the features are off, turn one on, then
# run the query that reaches queryRealisation. `_init_libstore_raw` is the
# unwrapped binding, which is precisely what the wrapper exists to stop callers
# from having to think about.
_ABORT_SCRIPT = """
import sys
import nanopynix

root, drv, entry = sys.argv[1], sys.argv[2], sys.argv[3]
getattr(nanopynix, entry)(load_config=False)
store = nanopynix.open_store("local://" + root)
nanopynix.enable_experimental_feature("ca-derivations")
store.query_missing([drv])
print("SURVIVED")
"""


async def _run_python(script: str, *args: str) -> CompletedProcess:
    return await run_process([sys.executable, "-c", script, *args])


@pytest.mark.parametrize("entry", ["init_nix", "init_libstore"])
async def test_init_entry_points_enable_the_default_experimental_features(entry: str) -> None:
    """Both entry points, because wrapping only one leaves the hazard reachable."""
    result = await _run_python(_REPORT_SCRIPT.format(entry=entry))

    assert result.returncode == 0, result.describe()
    enabled = set(result.stdout.split())
    assert set(DEFAULT_EXPERIMENTAL_FEATURES) <= enabled, (
        f"{entry} left these defaults off: {set(DEFAULT_EXPERIMENTAL_FEATURES) - enabled}"
    )


@pytest.mark.parametrize("entry", ["init_nix", "init_libstore"])
async def test_a_store_opened_after_init_survives_enabling_ca_derivations(
    entry: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The abort this ordering exists to prevent, exercised end to end.

    Non-vacuous by construction: it needs a *valid* derivation in the store,
    because ``query_missing`` on an unknown path answers from the path's absence
    and never reaches ``queryRealisation``. Against the unwrapped binding this
    exits with -6 (SIGABRT) and no traceback.
    """
    root = tmp_path_factory.mktemp("ca-ordering-store")
    expression = 'derivation { name = "probe"; system = builtins.currentSystem; builder = "/bin/sh"; }'
    instantiate = await run_process(["nix-instantiate", "--store", f"local://{root}", "--expr", expression])
    if instantiate.returncode != 0:
        pytest.fail(f"nix-instantiate {instantiate.describe()}")

    result = await _run_python(_ABORT_SCRIPT, str(root), instantiate.stdout.strip(), entry)

    assert result.returncode == 0, f"-6 is the SIGABRT this guards against; {result.describe()}"
    assert "SURVIVED" in result.stdout, result.describe()
