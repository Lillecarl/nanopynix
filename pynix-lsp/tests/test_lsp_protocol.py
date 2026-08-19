"""LSP requests over real JSON-RPC framing, with the server in this process.

These ran against a spawned ``pynix-lsp`` in ``test_lsp_e2e.py`` until issue
#44. The transport was the only thing that differed, and it was the thing that
cost the most: a request that did not answer left a bare 120-second timeout and
no way to ask the server what it was doing.

``lsp_wire`` gives the same ``pytest_lsp.LanguageClient``, the same
``client_capabilities("visual-studio-code")`` and the same JSON-RPC framing and
serialization over an in-memory duplex channel -- and it keeps a direct
reference to the server, so a failure here can read ``server.contexts[uri]``
rather than guess. The bodies below are unchanged from the versions that ran
over stdio.

**What stayed in ``test_lsp_e2e.py``** is what only a real process can answer:
that the packaged ``pynix-lsp`` entry point starts and negotiates
``initialize``. Nothing here duplicates that.

**A document round trip against a spawned server is not tested anywhere, and
issue #44 owns that gap.** It is the defect itself: the round trip is what
takes more than 120 s there, so a test of it today asserts the defect rather
than the behaviour. #44 asks for the test back, ungated, with the cause found.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from lsprotocol import types

from lsp_support.lsp_client import complete_at, definition_at, hover_at, open_document
from lsp_support.lsp_cursor import cursor_after

if TYPE_CHECKING:
    import pytest_lsp

    from pynix_lsp._handlers import PynixLanguageServer

_MODULE_SYSTEM = (Path(__file__).parent / "test_lsp" / "module_system").resolve()


@pytest.fixture
def wire_client(lsp_wire: tuple[PynixLanguageServer, pytest_lsp.LanguageClient]) -> pytest_lsp.LanguageClient:
    """The client half of the in-process pair. The server half is one attribute away."""
    _server, client = lsp_wire
    return client


async def test_bare_attrpath_completion_falls_back_to_the_options_tree(
    wire_client: pytest_lsp.LanguageClient,
) -> None:
    """A bare attrpath with no ``config``/``options`` prefix resolves against the declared options tree."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(wire_client, uri, source)
    await wire_client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(
        source,
        "services.example-daemon",
        needle="services.example-daemon.enable",
        offset=len("services.exam"),
    )
    completion = await complete_at(wire_client, uri, position)

    assert completion is not None
    items = completion.items if isinstance(completion, types.CompletionList) else completion
    labels = {item.label for item in items}
    assert labels == {"example-daemon"}


async def test_pkgs_completion_lists_real_nixpkgs_attributes(
    wire_client: pytest_lsp.LanguageClient,
) -> None:
    """``pkgs`` resolves through ``_module.args``, so this is real package completion."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(wire_client, uri, source)
    await wire_client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(
        source,
        "pkgs.hello",
        needle="programs.example.package = pkgs.hello",
        offset=len("pkgs.hel"),
    )
    completion = await complete_at(wire_client, uri, position)

    assert completion is not None
    items = completion.items if isinstance(completion, types.CompletionList) else completion
    labels = {item.label for item in items}
    assert "hello" in labels


async def test_hover_shows_the_declared_option(wire_client: pytest_lsp.LanguageClient) -> None:
    """Hovering a bare definition key shows the description the option declares."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(wire_client, uri, source)
    await wire_client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(source, "port", needle="services.example-daemon.port")
    hover = await hover_at(wire_client, uri, position)

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "Port the example daemon listens on." in hover.contents.value


async def test_go_to_definition_on_a_nixos_option_reference_lands_on_its_declaration(
    wire_client: pytest_lsp.LanguageClient,
) -> None:
    """``services.example-daemon.enable`` resolves to its ``declarationPositions`` in mod3.nix."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(wire_client, uri, source)
    await wire_client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(source, "enable", needle="services.example-daemon.enable")
    result = await definition_at(wire_client, uri, position)

    assert result is not None
    locations = [result] if isinstance(result, types.Location) else list(result)
    assert any(
        isinstance(location, types.Location)
        and location.uri.endswith("mod3.nix")
        and location.range.start == types.Position(3, 4)
        for location in locations
    )


async def test_renaming_a_let_binding_updates_every_reference(
    wire_client: pytest_lsp.LanguageClient,
) -> None:
    """Round-trips a real ``prepareRename`` plus ``rename`` against ``definition_scope.nix``'s outer ``greeting``."""
    path = _MODULE_SYSTEM / "definition_scope.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(wire_client, uri, source)
    await wire_client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(source, "greeting", needle='greeting = "hello"')

    prepared = await wire_client.text_document_prepare_rename_async(
        params=types.PrepareRenameParams(text_document=types.TextDocumentIdentifier(uri=uri), position=position),
    )
    assert prepared is not None

    edit = await wire_client.text_document_rename_async(
        params=types.RenameParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=position,
            new_name="salutation",
        ),
    )
    assert edit is not None
    assert edit.changes is not None
    text_edits = edit.changes[uri]
    assert len(text_edits) == 4
    assert {text_edit.new_text for text_edit in text_edits} == {"salutation"}
