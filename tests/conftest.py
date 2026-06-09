"""Test configuration and plugin registration for pynixd's test suite.

Responsibilities are split across ``tests/_conftest/`` submodules:

- **constants**: Stash keys, path prefixes, default configs, feature matrices.
- **helpers**: ``rmtree_robust``, ``rmtree_robust_glob``, ``run_subproc``.
- **config**: CLI options (``--client-bin``, ``--no-test-subsumption``), nix binary
  discovery, URI helpers, ``make_test_spec``, session-scoped ``pynixd_server``.
- **logging**: Structlog setup, ``_TestRelativeTimeHandler``, per-test log files,
  ``set_log_levels`` context manager.
- **subsumption**: ``_sort_by_subsumption``, ``pytest_runtest_protocol`` skip logic.
- **hooks**: ``pytest_collection_modifyitems``, ``pytest_runtest_makereport``,
  ``pytest_sessionstart``, ``pytest_terminal_summary``, ``pytest_ignore_collect``,
  async timeout wrapping, Lix skip handling.
- **fixtures**: ``clear_instrumentation``, ``_fixed_test_ts``, ``profiler``,
  ``cleanup_stores``, ``cleanup_extra_stores``, ``tmp_path`` override.
"""

from __future__ import annotations

# Re-export all hooks and fixtures so pytest discovers them.
from tests._conftest.config import (
    CLIENT_BIN as CLIENT_BIN,
)
from tests._conftest.config import (
    LIX_BIN as LIX_BIN,
)
from tests._conftest.config import (
    NIX_BIN as NIX_BIN,
)
from tests._conftest.config import (
    make_test_spec as make_test_spec,
)
from tests._conftest.config import (
    nix_env as nix_env,
)
from tests._conftest.config import (
    pynixd_server as pynixd_server,
)
from tests._conftest.config import (
    pytest_addoption as pytest_addoption,
)
from tests._conftest.config import (
    pytest_configure as pytest_configure,
)
from tests._conftest.config import (
    server_uri as server_uri,
)
from tests._conftest.config import (
    ssh_admin_uri as ssh_admin_uri,
)
from tests._conftest.config import (
    ssh_user_uri as ssh_user_uri,
)
from tests._conftest.config import (
    unix_session_uri as unix_session_uri,
)

# Re-export constants that test files import from conftest.
from tests._conftest.constants import (
    DEFAULT_SSH_OPTS as DEFAULT_SSH_OPTS,
)
from tests._conftest.constants import (
    SESSION_HTTP_PASS as SESSION_HTTP_PASS,
)
from tests._conftest.constants import (
    SESSION_HTTP_PORT as SESSION_HTTP_PORT,
)
from tests._conftest.constants import (
    SESSION_HTTP_USER as SESSION_HTTP_USER,
)
from tests._conftest.constants import (
    SESSION_SSH_PORT as SESSION_SSH_PORT,
)
from tests._conftest.constants import (
    SESSION_STORE_PREFIX as SESSION_STORE_PREFIX,
)
from tests._conftest.constants import (
    STORE_PREFIX as STORE_PREFIX,
)
from tests._conftest.constants import (
    TEST_NIX as TEST_NIX,
)
from tests._conftest.fixtures import (
    _fixed_test_ts as _fixed_test_ts,
)
from tests._conftest.fixtures import (
    cleanup_extra_stores as cleanup_extra_stores,
)
from tests._conftest.fixtures import (
    cleanup_stores as cleanup_stores,
)
from tests._conftest.fixtures import (
    clear_instrumentation as clear_instrumentation,
)
from tests._conftest.fixtures import (
    profiler as profiler,
)
from tests._conftest.fixtures import (
    tmp_path as tmp_path,
)
from tests._conftest.helpers import (
    rmtree_robust as rmtree_robust,
)
from tests._conftest.helpers import (
    rmtree_robust_glob as rmtree_robust_glob,
)
from tests._conftest.helpers import (
    run_subproc as run_subproc,
)
from tests._conftest.hooks import (
    pytest_collection_modifyitems as pytest_collection_modifyitems,
)
from tests._conftest.hooks import (
    pytest_ignore_collect as pytest_ignore_collect,
)
from tests._conftest.hooks import (
    pytest_runtest_makereport as pytest_runtest_makereport,
)
from tests._conftest.hooks import (
    pytest_sessionstart as pytest_sessionstart,
)
from tests._conftest.hooks import (
    pytest_terminal_summary as pytest_terminal_summary,
)
from tests._conftest.logging import (
    set_log_levels as set_log_levels,
)
from tests._conftest.logging import (
    test_log_dir as test_log_dir,
)
from tests._conftest.logging import (
    test_log_file as test_log_file,
)
from tests._conftest.subsumption import (
    pytest_runtest_protocol as pytest_runtest_protocol,
)
