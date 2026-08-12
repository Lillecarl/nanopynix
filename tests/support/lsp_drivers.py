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
from pynix._lsp._handlers import (
    _completion,
    _definition,
    _document_highlight,
    _hover,
    _prepare_rename,
    _references,
    _rename,
    _sync_document,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pynix._lsp._handlers import PynixLanguageServer
    from pytest_lsp import LanguageClient


class InProcessDriver:
    """Drives a ``PynixLanguageServer``'s handlers directly, no wire protocol."""

    def __init__(self, server: PynixLanguageServer) -> None:
        self._server = server
        self._versions: dict[str, int] = {}

    async def settle(self) -> None:
        """Wait for the warm-up that `_sync_document` starts and does not await.

        `_handlers._sync_document` runs `_warm_module_args` as a background
        task, and that is deliberate: the docstring above it says a real
        editor cannot wait for the module arguments of a file to resolve. So
        `_sync_document` returns while they are still unresolved, and a hover
        on one of them answers `None`.

        This driver calls that function directly, so it returns at once and
        the race is wide open. `WireDriver` needs no such wait, because a
        JSON-RPC round trip usually outlasts the warm-up. That is why the
        `[wire-...]` parametrisation of `tests/pynix/test_lsp_scenarios.py`
        stayed green while `[in_process-...]` failed, and why it failed on a
        loaded CI runner and never on a development machine.

        `tests/pynix/test_shared_eval_cache.py` waits the same way, for the
        same reason.
        """
        for task in list(self._server.warm_tasks):
            await task

    async def open(self, uri: str, text: str) -> None:
        self._versions[uri] = 1
        self._server.workspace.put_text_document(
            types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=text),
        )
        await _sync_document(self._server, uri)
        await self.settle()

    async def edit(self, uri: str, edit_range: types.Range, text: str) -> None:
        self._versions[uri] += 1
        self._server.workspace.update_text_document(
            types.VersionedTextDocumentIdentifier(uri=uri, version=self._versions[uri]),
            types.TextDocumentContentChangePartial(range=edit_range, text=text),
        )
        await _sync_document(self._server, uri)
        await self.settle()

    async def hover(self, uri: str, position: types.Position) -> types.Hover | None:
        return await _hover(self._server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    async def complete(self, uri: str, position: types.Position) -> types.CompletionList | None:
        return await _completion(self._server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    async def definition(self, uri: str, position: types.Position) -> types.Location | list[types.Location] | None:
        return await _definition(
            self._server,
            types.DefinitionParams(text_document=types.TextDocumentIdentifier(uri), position=position),
        )

    async def prepare_rename(self, uri: str, position: types.Position) -> types.PrepareRenameResult | None:
        return await _prepare_rename(
            self._server,
            types.PrepareRenameParams(text_document=types.TextDocumentIdentifier(uri), position=position),
        )

    async def rename(self, uri: str, position: types.Position, new_name: str) -> types.WorkspaceEdit | None:
        return await _rename(
            self._server,
            types.RenameParams(text_document=types.TextDocumentIdentifier(uri), position=position, new_name=new_name),
        )

    async def document_highlight(self, uri: str, position: types.Position) -> list[types.DocumentHighlight] | None:
        return await _document_highlight(
            self._server,
            types.DocumentHighlightParams(text_document=types.TextDocumentIdentifier(uri), position=position),
        )

    async def references(
        self,
        uri: str,
        position: types.Position,
        *,
        include_declaration: bool,
    ) -> list[types.Location] | None:
        return await _references(
            self._server,
            types.ReferenceParams(
                text_document=types.TextDocumentIdentifier(uri),
                position=position,
                context=types.ReferenceContext(include_declaration),
            ),
        )

    async def diagnostics(self, uri: str) -> Sequence[types.Diagnostic]:
        return self._server.diagnostics.get(uri, [])


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
                text_document=types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=text),
            ),
        )
        await self._client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    async def edit(self, uri: str, edit_range: types.Range, text: str) -> None:
        self._versions[uri] += 1
        self._client.text_document_did_change(
            types.DidChangeTextDocumentParams(
                text_document=types.VersionedTextDocumentIdentifier(uri=uri, version=self._versions[uri]),
                content_changes=[types.TextDocumentContentChangePartial(range=edit_range, text=text)],
            ),
        )
        await self._client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    async def hover(self, uri: str, position: types.Position) -> types.Hover | None:
        return await self._client.text_document_hover_async(
            params=types.HoverParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
        )

    async def complete(
        self,
        uri: str,
        position: types.Position,
    ) -> types.CompletionList | Sequence[types.CompletionItem] | None:
        return await self._client.text_document_completion_async(
            params=types.CompletionParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
        )

    async def definition(
        self,
        uri: str,
        position: types.Position,
    ) -> types.Location | Sequence[types.Location] | Sequence[types.LocationLink] | None:
        return await self._client.text_document_definition_async(
            params=types.DefinitionParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
        )

    async def prepare_rename(
        self,
        uri: str,
        position: types.Position,
    ) -> types.Range | types.PrepareRenamePlaceholder | types.PrepareRenameDefaultBehavior | None:
        return await self._client.text_document_prepare_rename_async(
            params=types.PrepareRenameParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
        )

    async def rename(self, uri: str, position: types.Position, new_name: str) -> types.WorkspaceEdit | None:
        return await self._client.text_document_rename_async(
            params=types.RenameParams(
                text_document=types.TextDocumentIdentifier(uri=uri),
                position=position,
                new_name=new_name,
            ),
        )

    async def document_highlight(
        self,
        uri: str,
        position: types.Position,
    ) -> Sequence[types.DocumentHighlight] | None:
        return await self._client.text_document_document_highlight_async(
            params=types.DocumentHighlightParams(
                text_document=types.TextDocumentIdentifier(uri=uri), position=position
            ),
        )

    async def references(
        self,
        uri: str,
        position: types.Position,
        *,
        include_declaration: bool,
    ) -> Sequence[types.Location] | None:
        return await self._client.text_document_references_async(
            params=types.ReferenceParams(
                text_document=types.TextDocumentIdentifier(uri=uri),
                position=position,
                context=types.ReferenceContext(include_declaration),
            ),
        )

    async def diagnostics(self, uri: str) -> Sequence[types.Diagnostic]:
        return self._client.diagnostics.get(uri, [])
