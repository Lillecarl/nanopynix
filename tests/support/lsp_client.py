"""Thin convenience wrappers around pytest-lsp's ``LanguageClient``.

Real e2e tests (see ``tests/pynix/test_lsp_e2e.py``) drive a genuine `pynix
lsp` subprocess over real stdio JSON-RPC. Without these, every test would
hand-roll ``DidOpenTextDocumentParams``/``CompletionParams``/``HoverParams``
boilerplate; these wrap that up so a test reads as "open this file, then
complete/hover at this position".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pytest_lsp import LanguageClient


def open_document(client: LanguageClient, uri: str, text: str, *, language_id: str = "nix", version: int = 1) -> None:
    """Send a real ``textDocument/didOpen`` notification."""
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(uri=uri, language_id=language_id, version=version, text=text),
        ),
    )


async def complete_at(
    client: LanguageClient,
    uri: str,
    position: types.Position,
) -> types.CompletionList | Sequence[types.CompletionItem] | None:
    """Send a real ``textDocument/completion`` request."""
    return await client.text_document_completion_async(
        params=types.CompletionParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
    )


async def hover_at(client: LanguageClient, uri: str, position: types.Position) -> types.Hover | None:
    """Send a real ``textDocument/hover`` request."""
    return await client.text_document_hover_async(
        params=types.HoverParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
    )


async def definition_at(
    client: LanguageClient,
    uri: str,
    position: types.Position,
) -> types.Location | Sequence[types.Location] | Sequence[types.LocationLink] | None:
    """Send a real ``textDocument/definition`` request."""
    return await client.text_document_definition_async(
        params=types.DefinitionParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
    )
