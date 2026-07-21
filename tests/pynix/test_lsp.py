# pyright: reportPrivateUsage=false
# Directly poking Workspace._workspace (via pygls' own private attribute) is
# the standard way to seed an in-memory workspace without a real stdio
# JSON-RPC handshake -- see pygls' own test suite for the same pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types
from pygls.workspace import Workspace
from pynix._lsp._context import parse_directive
from pynix._lsp._handlers import _completion, _hover, _sync_document, create_server
from pynix._lsp._syntax import completion_target_at, identifier_path_at, parse_errors, top_level_symbols

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.nix_environment import RpcSessionFactory


# ── parse_directive ──────────────────────────────────────────────────


def test_parse_directive_missing_returns_none() -> None:
    assert parse_directive("{ }\n") is None


def test_parse_directive_finds_a_well_formed_header() -> None:
    directive = parse_directive("# pynix-lsp: cfg = config.services.foo\n{ }\n")
    assert directive is not None
    assert directive.name == "cfg"
    assert directive.expr == "config.services.foo"


def test_parse_directive_allows_a_license_header_first() -> None:
    source = "# SPDX-License-Identifier: MIT\n\n# pynix-lsp: cfg = 1\n{ }\n"
    directive = parse_directive(source)
    assert directive is not None
    assert directive.name == "cfg"


def test_parse_directive_ignores_a_directive_past_the_scan_window() -> None:
    source = "\n".join(["# filler"] * 10 + ["# pynix-lsp: cfg = 1", "{ }"])
    assert parse_directive(source) is None


def test_parse_directive_rejects_a_malformed_line() -> None:
    assert parse_directive("# pynix-lsp: not a valid directive\n") is None


# ── tree-sitter syntax helpers ───────────────────────────────────────


def test_identifier_path_at_resolves_a_multi_segment_attrpath() -> None:
    source = "cfg.services.foo.enable"
    offset = source.index("services")
    assert identifier_path_at(source, offset) == ["cfg", "services"]


def test_identifier_path_at_resolves_the_final_segment() -> None:
    source = "cfg.services.foo.enable"
    offset = source.index("enable")
    assert identifier_path_at(source, offset) == ["cfg", "services", "foo", "enable"]


def test_identifier_path_at_resolves_a_bare_variable() -> None:
    source = "cfg"
    assert identifier_path_at(source, 1) == ["cfg"]


def test_identifier_path_at_returns_none_off_an_identifier() -> None:
    source = "1 + 2"
    assert identifier_path_at(source, 2) is None


def test_completion_target_at_splits_prefix_and_partial() -> None:
    source = "cfg.services.serv"
    assert completion_target_at(source, len(source)) == (["cfg", "services"], "serv")


def test_completion_target_at_handles_a_trailing_dot() -> None:
    source = "cfg.services."
    assert completion_target_at(source, len(source)) == (["cfg", "services"], "")


def test_completion_target_at_returns_none_with_no_identifier_before_cursor() -> None:
    source = "1 + "
    assert completion_target_at(source, len(source)) is None


def test_parse_errors_flags_broken_syntax() -> None:
    assert parse_errors("{ a = 1; b = ") != []


def test_parse_errors_is_empty_for_valid_syntax() -> None:
    assert parse_errors("{ a = 1; b = 2; }") == []


def test_top_level_symbols_lists_bindings() -> None:
    source = "{ options.services.foo.enable = true; config = { }; }"
    paths = [symbol.path for symbol in top_level_symbols(source)]
    assert paths == ["options.services.foo.enable", "config"]


# ── end-to-end against a real evaluator ──────────────────────────────


def _seed_document(workspace: Workspace, uri: str, source: str) -> None:
    workspace.put_text_document(types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=source))


async def test_hover_resolves_a_real_evaluated_value(rpc_session: RpcSessionFactory, tmp_path: Path) -> None:
    nix_file = tmp_path / "module.nix"
    source = "# pynix-lsp: cfg = { enable = true; nested = { value = 42; }; }\ncfg.nested.value\n"
    nix_file.write_text(source)
    uri = nix_file.as_uri()

    server = create_server()
    server.protocol._workspace = Workspace(root_uri=tmp_path.as_uri())
    _seed_document(server.workspace, uri, source)

    async with rpc_session() as nix:
        server.nix_session = nix
        store = nix.store()
        await store.open()
        server.store = store
        try:
            await _sync_document(server, uri)

            cursor = source.index("value", source.index("cfg.nested"))
            position = types.Position(line=1, character=cursor - len(source.splitlines()[0]) - 1)
            hover = await _hover(server, types.HoverParams(types.TextDocumentIdentifier(uri), position))
            assert hover is not None
            assert isinstance(hover.contents, types.MarkupContent)
            assert "42" in hover.contents.value
            assert "int" in hover.contents.value
        finally:
            await server.aclose()


async def test_completion_lists_real_attribute_names(rpc_session: RpcSessionFactory, tmp_path: Path) -> None:
    nix_file = tmp_path / "module.nix"
    source = '# pynix-lsp: cfg = { enable = true; extraConfig = ""; }\ncfg.\n'
    nix_file.write_text(source)
    uri = nix_file.as_uri()

    server = create_server()
    server.protocol._workspace = Workspace(root_uri=tmp_path.as_uri())
    _seed_document(server.workspace, uri, source)

    async with rpc_session() as nix:
        server.nix_session = nix
        store = nix.store()
        await store.open()
        server.store = store
        try:
            await _sync_document(server, uri)

            last_line = source.splitlines()[-1]
            position = types.Position(line=len(source.splitlines()) - 1, character=len(last_line))
            completion = await _completion(
                server, types.CompletionParams(types.TextDocumentIdentifier(uri), position)
            )
            assert completion is not None
            labels = {item.label for item in completion.items}
            assert labels == {"enable", "extraConfig"}
        finally:
            await server.aclose()


async def test_sync_document_reports_a_diagnostic_when_context_eval_fails(
    rpc_session: RpcSessionFactory, tmp_path: Path
) -> None:
    nix_file = tmp_path / "module.nix"
    source = '# pynix-lsp: cfg = throw "boom"\ncfg\n'
    nix_file.write_text(source)
    uri = nix_file.as_uri()

    server = create_server()
    server.protocol._workspace = Workspace(root_uri=tmp_path.as_uri())
    _seed_document(server.workspace, uri, source)

    published: list[types.PublishDiagnosticsParams] = []
    server.text_document_publish_diagnostics = published.append  # type: ignore[method-assign] -- swap the real RPC notification for a test recorder

    async with rpc_session() as nix:
        server.nix_session = nix
        store = nix.store()
        await store.open()
        server.store = store
        try:
            await _sync_document(server, uri)
        finally:
            await server.aclose()

    assert published
    messages = [d.message for d in published[-1].diagnostics]
    assert any("boom" in message for message in messages)


async def test_context_expression_resolves_relative_to_the_files_own_directory(
    rpc_session: RpcSessionFactory, tmp_path: Path
) -> None:
    (tmp_path / "shared.nix").write_text("{ value = 7; }\n")
    nix_file = tmp_path / "module.nix"
    source = "# pynix-lsp: cfg = import ./shared.nix\ncfg.value\n"
    nix_file.write_text(source)
    uri = nix_file.as_uri()

    server = create_server()
    server.protocol._workspace = Workspace(root_uri=tmp_path.as_uri())
    _seed_document(server.workspace, uri, source)

    async with rpc_session() as nix:
        server.nix_session = nix
        store = nix.store()
        await store.open()
        server.store = store
        try:
            await _sync_document(server, uri)
            context = server.contexts[uri]
            assert context.error is None
            assert context.root is not None
        finally:
            await server.aclose()
