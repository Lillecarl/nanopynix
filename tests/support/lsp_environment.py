"""Shared PynixLanguageServer test fixtures, wired to a real evaluator.

Mirrors ``nanopynix_testing.nix_environment``'s pattern for reusable test
infrastructure: any ``test_lsp_*.py`` file can request ``lsp_server`` (or
``lsp_wire``) without re-deriving the pygls ``Workspace``/``Session``/
``Store`` wiring.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_lsp
from lsprotocol import types
from pygls.io_ import run_async
from pygls.workspace import Workspace
from pynix._lsp._handlers import create_server
from pytest_lsp import client_capabilities

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pynix._lsp._handlers import PynixLanguageServer

    from nanopynix_testing.nix_environment import RpcSessionFactory

LSP_ASSETS_ROOT = Path(__file__).resolve().parents[1] / "pynix" / "test_lsp"
_logger = logging.getLogger(__name__)


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
        root_uri=LSP_ASSETS_ROOT.as_uri(),
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


class _FeedWriter:
    """A pygls writer that feeds its bytes straight into the other side's ``StreamReader``.

    This is the whole trick behind connecting a real ``pytest_lsp``
    ``LanguageClient`` to a real ``PynixLanguageServer`` without a
    subprocess, socket, or OS pipe: ``asyncio.StreamReader.feed_data`` is a
    public method exactly meant for driving a reader without a real
    transport underneath it, so two of them (one per direction) are a
    complete in-memory duplex JSON-RPC channel.
    """

    def __init__(self, target: asyncio.StreamReader) -> None:
        self._target = target

    def write(self, data: bytes) -> None:
        self._target.feed_data(data)

    def close(self) -> None:
        self._target.feed_eof()


@pytest.fixture
async def lsp_wire(
    rpc_session: RpcSessionFactory,
) -> AsyncIterator[tuple[PynixLanguageServer, pytest_lsp.LanguageClient]]:
    """A real ``pytest_lsp.LanguageClient`` <-> ``PynixLanguageServer`` pair, wired in-process.

    Unlike ``test_lsp_e2e.py``'s subprocess-based client, both sides run in
    this same event loop over an in-memory duplex channel (see
    ``_FeedWriter``) -- real JSON-RPC framing/serialization end to end
    (catches bugs the in-process ``lsp_server`` fixture structurally can't),
    while still holding a direct reference to the server for state
    introspection (e.g. ``server.contexts[uri]``).
    """
    server = create_server()
    server.protocol._workspace = Workspace(  # type: ignore[reportPrivateUsage] -- see lsp_server's comment above
        root_uri=LSP_ASSETS_ROOT.as_uri(),
    )
    client = pytest_lsp.make_test_lsp_client()

    to_server = asyncio.StreamReader()
    to_client = asyncio.StreamReader()
    server.protocol.set_writer(_FeedWriter(to_client))
    client.protocol.set_writer(_FeedWriter(to_server))

    stop_event = threading.Event()
    server_task = asyncio.create_task(
        run_async(stop_event=stop_event, reader=to_server, protocol=server.protocol, logger=_logger),
    )
    client_task = asyncio.create_task(
        run_async(stop_event=stop_event, reader=to_client, protocol=client.protocol, logger=_logger),
    )

    async with rpc_session() as nix:
        store = nix.store()
        await store.open()
        server.nix_session = nix
        server.store = store
        try:
            await client.initialize_session(
                types.InitializeParams(
                    capabilities=client_capabilities("visual-studio-code"),
                    workspace_folders=[types.WorkspaceFolder(uri=LSP_ASSETS_ROOT.as_uri(), name="test_lsp")],
                ),
            )
            yield server, client
            await client.shutdown_session()
        finally:
            stop_event.set()
            to_server.feed_eof()
            to_client.feed_eof()
            await asyncio.gather(server_task, client_task)
            await server.aclose()
