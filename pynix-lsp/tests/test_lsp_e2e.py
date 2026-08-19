"""What only a real `pynix-lsp` process can answer.

These spawn the packaged entry point and speak genuine LSP JSON-RPC to it, so
they catch what neither in-process tier can: that the installed program starts
at all, and that its `initialize` response advertises what a client needs --
the completion trigger character is the example, because a client has no reason
to ask for completions again the instant you type `.` unless the server said it
should.

**The behavioural tests are in `test_lsp_protocol.py`, and they are not a
lesser tier.** `lsp_wire` gives the same `pytest_lsp.LanguageClient`, the same
`client_capabilities("visual-studio-code")`, and the same JSON-RPC framing and
serialization -- over an in-memory duplex channel, with both halves in one
process. Only the transport differs, and the transport is what made a failure
unreadable here: a request that did not answer left a 120-second timeout and no
way to ask the server anything. The same five assertions take 3.4 s there and
took 10 minutes of deadlines here. Issue #44.

So this module stays small on purpose. Add a test here only when a real process
is what the test is about.
"""

from __future__ import annotations

# `AsyncIterator` is a real (non-TYPE_CHECKING) import here, unlike this
# repo's usual convention: pytest_lsp's fixture wrapper calls
# `typing.get_type_hints()` on `client` below *at runtime* to find the
# parameter that needs the injected client, which requires every name in its
# signature to actually resolve at import time despite `from __future__
# import annotations` making the annotations themselves lazy strings.
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_lsp
from lsprotocol import types
from pytest_lsp import ClientServerConfig, LanguageClient, client_capabilities

_MODULE_SYSTEM = (Path(__file__).parent / "test_lsp" / "module_system").resolve()


@pytest_lsp.fixture(  # type: ignore[reportUnknownMemberType] -- pytest_lsp.fixture's decorator-factory return type isn't fully resolvable by pyright
    scope="module",
    config=ClientServerConfig(server_command=["pynix-lsp"]),
)
async def client(lsp_client: LanguageClient) -> AsyncIterator[None]:
    response = await lsp_client.initialize_session(
        types.InitializeParams(
            capabilities=client_capabilities("visual-studio-code"),
            workspace_folders=[types.WorkspaceFolder(uri=_MODULE_SYSTEM.as_uri(), name="module_system")],
        ),
    )
    # pytest_lsp's fixture machinery injects `lsp_client` itself as the
    # `client` fixture value (the bare `yield` above is not what tests
    # receive) -- stash the server's response on it so tests can reach it.
    lsp_client.server_capabilities = response.capabilities  # type: ignore[attr-defined] -- dynamically stashed, see comment above
    try:
        yield
    finally:
        _report_server_stderr(lsp_client)
        await lsp_client.shutdown_session()


def _report_server_stderr(lsp_client: LanguageClient) -> None:
    """Put whatever the server wrote to stderr into the report of the test.

    **A failure here used to arrive with nothing to read.** The client waits
    for a message, the deadline fires, and the traceback names the wait. What
    the server was doing went to its stderr, which nothing collected, so
    issue #44 took six experiments by hand to state.

    `pytest_lsp` keeps the lines in `LanguageClient.stderr`, under one name or
    another depending on the version, so this asks for each and reports the
    first that answers. It prints rather than logs: a print lands in the
    captured output of the test, which pytest shows on a failure and
    pytest-agent writes to the detail file either way.
    """
    for attribute in ("stderr", "server_stderr", "_stderr"):
        captured: object = getattr(lsp_client, attribute, None)
        if captured is None:
            continue
        text = (
            "".join(str(line) for line in cast("list[object]", captured))
            if isinstance(captured, list)
            else str(captured)
        )
        if text.strip():
            print(f"--- stderr of the `pynix-lsp` server ({attribute}) ---\n{text}")  # noqa: T201 -- the report is the point
        return


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
async def test_server_advertises_definition_rename_highlight_and_references_capabilities(
    client: LanguageClient,
) -> None:
    """Without these, a real client has no reason to ever send the corresponding requests at all."""
    capabilities: types.ServerCapabilities = client.server_capabilities  # type: ignore[attr-defined] -- see `client` fixture
    assert capabilities.definition_provider  # type: ignore[reportUnknownMemberType] -- lsprotocol's generated *Options union types aren't fully resolvable by pyright
    assert capabilities.rename_provider  # type: ignore[reportUnknownMemberType] -- see above
    assert capabilities.document_highlight_provider  # type: ignore[reportUnknownMemberType] -- see above
    assert capabilities.references_provider  # type: ignore[reportUnknownMemberType] -- see above
