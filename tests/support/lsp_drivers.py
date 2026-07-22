"""Concrete ``LspDriver`` implementations for ``lsp_scenario.Scenario``.

Two backends, same ``Scenario``/``Action`` list:

- ``InProcessDriver`` -- calls ``pynix._lsp._handlers``' handler functions
  directly, no wire protocol at all (fastest, matches ``test_lsp.py``'s
  existing style).
- ``WireDriver`` -- speaks real JSON-RPC via a ``pytest_lsp.LanguageClient``,
  whether that client is connected to an in-memory server (``lsp_wire``
  fixture) or a real subprocess (``test_lsp_e2e.py``'s style) -- both are the
  same driver, since the client doesn't know or care how its transport was
  established.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types
from pynix._lsp._handlers import _completion, _hover, _sync_document

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pynix._lsp._handlers import PynixLanguageServer
    from pytest_lsp import LanguageClient


class InProcessDriver:
    """Drives a ``PynixLanguageServer``'s handlers directly, no wire protocol."""

    def __init__(self, server: PynixLanguageServer) -> None:
        self._server = server
        self._versions: dict[str, int] = {}

    async def open(self, uri: str, text: str) -> None:
        self._versions[uri] = 1
        self._server.workspace.put_text_document(
            types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=text)
        )
        await _sync_document(self._server, uri)

    async def edit(self, uri: str, edit_range: types.Range, text: str) -> None:
        self._versions[uri] += 1
        self._server.workspace.update_text_document(
            types.VersionedTextDocumentIdentifier(uri=uri, version=self._versions[uri]),
            types.TextDocumentContentChangePartial(range=edit_range, text=text),
        )
        await _sync_document(self._server, uri)

    async def hover(self, uri: str, position: types.Position) -> types.Hover | None:
        return await _hover(self._server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    async def complete(self, uri: str, position: types.Position) -> types.CompletionList | None:
        return await _completion(self._server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))


class WireDriver:
    """Drives a real ``pytest_lsp.LanguageClient`` over genuine JSON-RPC.

    Backend-agnostic: works whether *client* is wired to an in-memory server
    (``lsp_wire`` fixture) or a real ``pynix lsp`` subprocess (like
    ``test_lsp_e2e.py``) -- the client API is identical either way.
    """

    def __init__(self, client: LanguageClient) -> None:
        self._client = client
        self._versions: dict[str, int] = {}

    async def open(self, uri: str, text: str) -> None:
        self._versions[uri] = 1
        self._client.text_document_did_open(
            types.DidOpenTextDocumentParams(
                text_document=types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=text)
            )
        )
        await self._client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    async def edit(self, uri: str, edit_range: types.Range, text: str) -> None:
        self._versions[uri] += 1
        self._client.text_document_did_change(
            types.DidChangeTextDocumentParams(
                text_document=types.VersionedTextDocumentIdentifier(uri=uri, version=self._versions[uri]),
                content_changes=[types.TextDocumentContentChangePartial(range=edit_range, text=text)],
            )
        )
        await self._client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    async def hover(self, uri: str, position: types.Position) -> types.Hover | None:
        return await self._client.text_document_hover_async(
            params=types.HoverParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position)
        )

    async def complete(
        self, uri: str, position: types.Position
    ) -> types.CompletionList | Sequence[types.CompletionItem] | None:
        return await self._client.text_document_completion_async(
            params=types.CompletionParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position)
        )
