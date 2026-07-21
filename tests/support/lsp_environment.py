"""Shared PynixLanguageServer test fixture, wired to a real evaluator.

Mirrors ``tests/support/nix_environment.py``'s pattern for reusable test
infrastructure: any ``test_lsp_*.py`` file can request ``lsp_server`` without
re-deriving the pygls ``Workspace``/``Session``/``Store`` wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pygls.workspace import Workspace
from pynix._lsp._handlers import create_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pynix._lsp._handlers import PynixLanguageServer

    from tests.support.nix_environment import RpcSessionFactory

LSP_ASSETS_ROOT = Path(__file__).resolve().parents[1] / "pynix" / "test_lsp"


def asset(name: str) -> Path:
    """Path to a checked-in LSP test fixture under ``tests/pynix/test_lsp/``."""
    return LSP_ASSETS_ROOT / name


@pytest.fixture
async def lsp_server(rpc_session: RpcSessionFactory) -> AsyncIterator[PynixLanguageServer]:
    """A PynixLanguageServer wired to a real evaluator and the test_lsp/ workspace."""
    server = create_server()
    # pygls exposes no public way to seed a Workspace without a real stdio
    # handshake -- see pygls' own test suite for the same pattern.
    server.protocol._workspace = Workspace(  # type: ignore[reportPrivateUsage] -- intentional, see comment above
        root_uri=LSP_ASSETS_ROOT.as_uri()
    )
    async with rpc_session() as nix:
        store = nix.store()
        await store.open()
        server.nix_session = nix
        server.store = store
        try:
            yield server
        finally:
            await server.aclose()
