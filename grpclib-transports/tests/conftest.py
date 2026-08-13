"""What this suite needs on top of the shared layer, which is very little.

``pytest.ini`` registers ``test_support.plugin``, so the per-test deadline,
the hang report behind it and the stand-in for pytest-agent's ``agent_notes``
arrive from there. Issue #130 is what made that possible: the helpers used to
be under ``tests/support/``, which resolves from the repository rootdir alone,
so this suite could reach none of them and wrote its own copy of the sandbox
check below.

The nanopynix layer stays out. This suite loads no ``nix_environment``, no
beartype instrumentation over ``nanopynix``, and none of the nine markers of
that library. ``pytest.ini`` gives the reason to keep it that way.
"""

from __future__ import annotations

import logging

import pytest

from test_support.environment import in_nix_build_sandbox

logging.getLogger("h2").setLevel(logging.WARNING)
logging.getLogger("asyncssh").setLevel(logging.WARNING)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the tests that bind a TCP socket, when nothing can bind one.

    A conftest hook and the plugin's hook of the same name both run, so the
    deadline that ``test_support.plugin`` applies is not lost here.
    """
    if not in_nix_build_sandbox():
        return

    skip_tcp = pytest.mark.skip(reason="TCP binding is unavailable in the Nix build sandbox")
    for item in items:
        if item.get_closest_marker("tcp") is not None:
            item.add_marker(skip_tcp)
