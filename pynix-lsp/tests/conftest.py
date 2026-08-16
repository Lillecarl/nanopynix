"""Fixtures for the language-server suite.

`pynix/tests/conftest.py` held these until issue #107 moved the server into
its own project. Two of them are the server itself, and the third realises an
asset that only these scenarios read, so none of them belongs to `pynix` any
more.

**The rest of the harness arrives as a plugin.** `nix_backend`, `rpc_session`
and the store fixtures come from `nanopynix_testing`, which every suite here
installs, so this file does not repeat them. Issue #130 tracks the wider
question of a shared test setup; nothing here waits on it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lsp_support.lsp_environment import lsp_server as lsp_server, lsp_wire as lsp_wire

# Session scope, and anyio's own `anyio_backend` is module scope. Both are
# global plugins because `pytest.ini` registers ours with `-p`, and anyio wins
# that tie -- every session-scoped fixture that requests it then fails, which
# `shared_nix_environment` does. A conftest beats any plugin, so importing the
# fixture here settles it. `pynix/tests/conftest.py` and
# `nanopynix/tests/conftest.py` carry the same import and the full account.
from nanopynix_testing.nix_environment import anyio_backend as anyio_backend


@pytest.fixture(scope="session")
def easykubenix_openapi_schema() -> str:
    """Realise the pinned Kubernetes OpenAPI schema the easykubenix scenarios read.

    The entry point is found relative to this file rather than to ``repo_root``.
    Issue #130 moved the suite, and a path spelled out from the repository root
    is a second place that has to be edited when it moves again.

    ``test_lsp/easykubenix/default.nix`` beside this file interpolates a ``fetchurl``
    derivation into ``openApiSchemaPath``, and nothing on the path from there to
    the assertion ever *builds* it -- the LSP evaluates that file and then reads
    the resulting path straight off disk. So on any machine where the fetch has
    not happened to occur, all nine easykubenix scenarios fail with
    ``FileNotFoundError`` on a store path that is perfectly valid and simply
    absent, which reads as a bug in the LSP rather than a missing fixture.

    Sync on purpose: a blocking call is fine here (this is not an async
    function), and a session-scoped async fixture would need the whole suite's
    event loop to outlive it for no benefit.
    """
    entry = Path(__file__).parent / "test_lsp" / "easykubenix" / "default.nix"
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, paths from the repo itself
        ["nix-build", "--no-out-link", str(entry), "-A", "openApiSchema"],  # noqa: S607 -- nix-build resolved from PATH, as everywhere else in this suite
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not realise the easykubenix OpenAPI schema (offline?):\n{result.stderr}")
    return result.stdout.strip()
