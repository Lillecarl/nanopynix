"""Real end-to-end tests for `pynix lsp` over actual stdio JSON-RPC.

Unlike test_lsp.py (which drives the handler functions directly, in-process,
against a real evaluator), these spawn the real `pynix lsp` subprocess and
speak genuine LSP JSON-RPC to it via pytest-lsp -- catching bugs that only
show up in real protocol negotiation, like a missing completion trigger
character (a client has no reason to ask for completions again the instant
you type `.` unless the server's `initialize` response advertises it).
"""

from __future__ import annotations

# `AsyncIterator` is a real (non-TYPE_CHECKING) import here, unlike this
# repo's usual convention: pytest_lsp's fixture wrapper calls
# `typing.get_type_hints()` on `client` below *at runtime* to find the
# parameter that needs the injected client, which requires every name in its
# signature to actually resolve at import time despite `from __future__
# import annotations` making the annotations themselves lazy strings.
from collections.abc import AsyncIterator  # noqa: TC003 -- must stay a real import, see comment above
from pathlib import Path

import pytest
import pytest_lsp
from lsprotocol import types
from pytest_lsp import ClientServerConfig, LanguageClient, client_capabilities

from tests.support.lsp_client import complete_at, hover_at, open_document
from tests.support.lsp_cursor import cursor_after

_MODULE_SYSTEM = (Path(__file__).parent / "test_lsp" / "module_system").resolve()


@pytest_lsp.fixture(  # type: ignore[reportUnknownMemberType] -- pytest_lsp.fixture's decorator-factory return type isn't fully resolvable by pyright
    scope="module",
    config=ClientServerConfig(server_command=["pynix", "lsp"]),
)
async def client(lsp_client: LanguageClient) -> AsyncIterator[None]:
    response = await lsp_client.initialize_session(
        types.InitializeParams(
            capabilities=client_capabilities("visual-studio-code"),
            workspace_folders=[types.WorkspaceFolder(uri=_MODULE_SYSTEM.as_uri(), name="module_system")],
        )
    )
    # pytest_lsp's fixture machinery injects `lsp_client` itself as the
    # `client` fixture value (the bare `yield` above is not what tests
    # receive) -- stash the server's response on it so tests can reach it.
    lsp_client.server_capabilities = response.capabilities  # type: ignore[attr-defined] -- dynamically stashed, see comment above
    yield
    await lsp_client.shutdown_session()


@pytest.mark.asyncio(loop_scope="module")
async def test_completion_advertises_dot_as_a_trigger_character(client: LanguageClient) -> None:
    """Without this, a client has no reason to re-request completions right after typing `.`."""
    capabilities: types.ServerCapabilities = client.server_capabilities  # type: ignore[attr-defined] -- see `client` fixture
    provider = capabilities.completion_provider  # type: ignore[reportUnknownVariableType] -- lsprotocol's generated CompletionOptions union type isn't fully resolvable by pyright
    assert provider is not None
    trigger_characters: list[str] | None = provider.trigger_characters  # type: ignore[reportUnknownMemberType] -- see above
    assert trigger_characters is not None
    assert "." in trigger_characters


@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_bare_attrpath_completion_falls_back_to_the_options_tree(client: LanguageClient) -> None:
    """Same scenario as test_lsp.py's in-process version, but over real stdio JSON-RPC."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(client, uri, source)
    await client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(
        source, "services.example-daemon", needle="services.example-daemon.enable", offset=len("services.exam")
    )
    completion = await complete_at(client, uri, position)

    assert completion is not None
    items = completion.items if isinstance(completion, types.CompletionList) else completion
    labels = {item.label for item in items}
    assert labels == {"example-daemon"}


@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_pkgs_completion_lists_real_nixpkgs_attributes(client: LanguageClient) -> None:
    """Same scenario as test_lsp.py's in-process version, but over real stdio JSON-RPC."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(client, uri, source)
    await client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(
        source, "pkgs.hello", needle="programs.example.package = pkgs.hello", offset=len("pkgs.hel")
    )
    completion = await complete_at(client, uri, position)

    assert completion is not None
    items = completion.items if isinstance(completion, types.CompletionList) else completion
    labels = {item.label for item in items}
    assert "hello" in labels


@pytest.mark.asyncio(loop_scope="module")
async def test_e2e_hover_shows_the_declared_option(client: LanguageClient) -> None:
    """Same scenario as test_lsp.py's in-process version, but over real stdio JSON-RPC."""
    path = _MODULE_SYSTEM / "config1.nix"
    source = path.read_text()
    uri = path.as_uri()
    open_document(client, uri, source)
    await client.wait_for_notification(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)

    position = cursor_after(source, "port", needle="services.example-daemon.port")
    hover = await hover_at(client, uri, position)

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "Port the example daemon listens on." in hover.contents.value
