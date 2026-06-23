"""Test configuration and plugin registration for pynixd's test suite.

Split across ``tests/_conftest/`` submodules — see module docstrings there.
"""

from __future__ import annotations

from tests._conftest.config import (
    CLIENT_BIN,
    LIX_BIN,
    NIX_BIN,
    make_test_spec,
    nix_env,
    pynixd_server,
    pytest_addoption,
    pytest_configure,
    server_uri,
    ssh_admin_uri,
    ssh_user_uri,
    unix_session_uri,
)
from tests._conftest.constants import (
    DEFAULT_SSH_OPTS,
    SESSION_HTTP_PASS,
    SESSION_HTTP_PORT,
    SESSION_HTTP_USER,
    SESSION_SSH_PORT,
    SESSION_STORE_PREFIX,
    STORE_PREFIX,
    TEST_NIX,
)
from tests._conftest.fixtures import (
    _fixed_test_ts,
    anyio_backend,
    cleanup_extra_stores,
    cleanup_stores,
    clear_instrumentation,
    profiler,
    tmp_path,
)
from tests._conftest.helpers import rmtree_robust, rmtree_robust_glob, run_subproc
from tests._conftest.hooks import (
    pytest_collection_modifyitems,
    pytest_ignore_collect,
    pytest_runtest_makereport,
    pytest_sessionstart,
    pytest_terminal_summary,
)
from tests._conftest.logging import (
    configure_test_logging,
    revive_messages_containing,
    revive_messages_matching,
    set_log_levels,
    suppress_messages_containing,
    suppress_messages_matching,
    test_log_dir,
    test_logging,
)
from tests._conftest.subsumption import pytest_runtest_protocol

__all__ = [
    "_fixed_test_ts",
    "anyio_backend",
    "cleanup_extra_stores",
    "cleanup_stores",
    "clear_instrumentation",
    "CLIENT_BIN",
    "configure_test_logging",
    "DEFAULT_SSH_OPTS",
    "LIX_BIN",
    "make_test_spec",
    "NIX_BIN",
    "nix_env",
    "profiler",
    "pytest_addoption",
    "pytest_collection_modifyitems",
    "pytest_configure",
    "pytest_ignore_collect",
    "pytest_runtest_makereport",
    "pytest_runtest_protocol",
    "pytest_sessionstart",
    "pytest_terminal_summary",
    "pynixd_server",
    "revive_messages_containing",
    "revive_messages_matching",
    "rmtree_robust",
    "rmtree_robust_glob",
    "run_subproc",
    "server_uri",
    "SESSION_HTTP_PASS",
    "SESSION_HTTP_PORT",
    "SESSION_HTTP_USER",
    "SESSION_SSH_PORT",
    "SESSION_STORE_PREFIX",
    "set_log_levels",
    "ssh_admin_uri",
    "ssh_user_uri",
    "STORE_PREFIX",
    "suppress_messages_containing",
    "suppress_messages_matching",
    "test_log_dir",
    "test_logging",
    "TEST_NIX",
    "tmp_path",
    "unix_session_uri",
]
