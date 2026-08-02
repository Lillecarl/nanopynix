from __future__ import annotations

import logging
import os

import pytest

logging.getLogger("h2").setLevel(logging.WARNING)
logging.getLogger("asyncssh").setLevel(logging.WARNING)


def _in_nix_build_sandbox() -> bool:
    return "NIX_BUILD_TOP" in os.environ or os.environ.get("IN_NIX_SHELL") == "pure"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if not _in_nix_build_sandbox():
        return

    skip_tcp = pytest.mark.skip(reason="TCP binding is unavailable in the Nix build sandbox")
    for item in items:
        if item.get_closest_marker("tcp") is not None:
            item.add_marker(skip_tcp)
