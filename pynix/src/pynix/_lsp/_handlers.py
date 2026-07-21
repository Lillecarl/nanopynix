"""pygls request handlers for pynix's Nix language server."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

import nanopynix
from nanopynix.exceptions import NixError
from pynix._lsp._context import FileContext, parse_directive
from pynix._lsp._syntax import completion_target_at, identifier_path_at, parse_errors, top_level_symbols

if TYPE_CHECKING:
    from pynix._lsp._syntax import ParseErrorRange

_SERVER_NAME = "pynix-lsp"
_SERVER_VERSION = "0.1.0"


class PynixLanguageServer(LanguageServer):
    """Owns the one shared Nix evaluator worker for the whole server session.

    Each open file with a ``# pynix-lsp:`` directive gets its own
    ``FileContext`` (and so its own ``EvalSession``) within this one shared
    ``Session``/``Store`` -- opening a new ``EvalSession`` is cheap (a
    dedicated thread in the existing worker), unlike spawning a whole new
    worker subprocess per file.
    """

    def __init__(self) -> None:
        super().__init__(_SERVER_NAME, _SERVER_VERSION)  # type: ignore[reportUnknownMemberType] -- pygls' LanguageServer.__init__ forwards *args/**kwargs, so pyright can't fully resolve its composed signature
        self.nix_session: nanopynix.Session | None = None
        self.store: nanopynix.Store | None = None
        self.contexts: dict[str, FileContext] = {}

    async def ensure_nix(self) -> tuple[nanopynix.Session, nanopynix.Store]:
        """Open the shared Session/Store on first use."""
        if self.nix_session is not None and self.store is not None:
            return self.nix_session, self.store
        session = nanopynix.Session(experimental_features=["flakes", "nix-command"])
        await session.open()
        store = session.store()
        await store.open()
        self.nix_session = session
        self.store = store
        return session, store

    async def aclose(self) -> None:
        """Close every open file context and the shared Session/Store."""
        for context in self.contexts.values():
            await context.close()
        self.contexts.clear()
        if self.store is not None:
            await self.store.close()
            self.store = None
        if self.nix_session is not None:
            await self.nix_session.close()
            self.nix_session = None


def _byte_offset(source: str, position: types.Position, ls: PynixLanguageServer, uri: str) -> int:
    document = ls.workspace.get_text_document(uri)
    char_offset = document.offset_at_position(position)
    return len(source[:char_offset].encode("utf-8"))


def _parse_error_diagnostic(error: ParseErrorRange) -> types.Diagnostic:
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(error.start_row, error.start_column),
            end=types.Position(error.end_row, error.end_column),
        ),
        message=error.message,
        severity=types.DiagnosticSeverity.Error,
        source=_SERVER_NAME,
    )


def _context_error_diagnostic(error: NixError) -> types.Diagnostic:
    zero = types.Position(0, 0)
    return types.Diagnostic(
        range=types.Range(start=zero, end=zero),
        message=f"pynix-lsp context failed to evaluate: {error.msg_without_ansi}",
        severity=types.DiagnosticSeverity.Error,
        source=_SERVER_NAME,
    )


async def _sync_document(ls: PynixLanguageServer, uri: str) -> None:
    """Reconcile one document's context against its current header directive.

    Only (re)evaluates the context expression when the directive text itself
    changed -- routine edits to the rest of the file just get fresh parse
    diagnostics, matching nixd's own "assume an evaluated value doesn't
    change until told otherwise" caching stance.
    """
    document = ls.workspace.get_text_document(uri)
    source = document.source
    directive = parse_directive(source)
    context = ls.contexts.get(uri)

    if directive is None:
        if context is not None:
            await context.close()
            del ls.contexts[uri]
        context = None
    elif context is None or context.directive != directive:
        file_path = to_fs_path(uri)
        file_dir = file_path.rsplit("/", 1)[0] if file_path and "/" in file_path else "."
        session, store = await ls.ensure_nix()
        context = FileContext(session, store, directive, file_dir)
        await context.reload()
        ls.contexts[uri] = context

    diagnostics = [_parse_error_diagnostic(error) for error in parse_errors(source)]
    if context is not None and context.error is not None:
        diagnostics.append(_context_error_diagnostic(context.error))
    ls.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics))


async def _resolve_path(context: FileContext, path: list[str]) -> nanopynix.ValueProxy | None:
    """Walk *path* through *context*'s root, or None if it isn't rooted there."""
    if context.root is None or not path or path[0] != context.directive.name:
        return None
    value = context.root
    for segment in path[1:]:
        value = value.attr(segment)
    return value


async def _render_value(value: nanopynix.ValueProxy) -> str:
    nix_type = await value.get_type()
    sections = [f"```\n{nix_type.name.lower()}\n```"]
    if nix_type != nanopynix.NixType.FUNCTION:
        try:
            json_value = await value.force_json()
        except NixError:
            pass
        else:
            sections.append(f"```json\n{json.dumps(json_value, indent=2, sort_keys=True)}\n```")
    try:
        edit_path, edit_line = await value.edit_location()
    except NixError:
        pass
    else:
        if edit_path:
            sections.append(f"defined at `{edit_path}:{edit_line}`")
    return "\n\n".join(sections)


async def _hover(ls: PynixLanguageServer, params: types.HoverParams) -> types.Hover | None:
    uri = params.text_document.uri
    context = ls.contexts.get(uri)
    if context is None:
        return None
    document = ls.workspace.get_text_document(uri)
    source = document.source
    byte_offset = _byte_offset(source, params.position, ls, uri)
    path = identifier_path_at(source, byte_offset)
    if path is None:
        return None
    value = await _resolve_path(context, path)
    if value is None:
        return None
    try:
        rendered = await _render_value(value)
    except NixError:
        return None
    return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=rendered))


async def _completion(ls: PynixLanguageServer, params: types.CompletionParams) -> types.CompletionList | None:
    uri = params.text_document.uri
    context = ls.contexts.get(uri)
    if context is None:
        return None
    document = ls.workspace.get_text_document(uri)
    source = document.source
    byte_offset = _byte_offset(source, params.position, ls, uri)
    target = completion_target_at(source, byte_offset)
    if target is None:
        return None
    prefix, partial = target
    value = await _resolve_path(context, prefix)
    if value is None:
        return None
    try:
        names = await value.attr_names()
    except NixError:
        return None
    items = [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
    return types.CompletionList(is_incomplete=False, items=items)


def _document_symbols(ls: PynixLanguageServer, uri: str) -> list[types.DocumentSymbol]:
    document = ls.workspace.get_text_document(uri)
    symbols: list[types.DocumentSymbol] = []
    for symbol in top_level_symbols(document.source):
        symbol_range = types.Range(
            start=types.Position(symbol.start_row, symbol.start_column),
            end=types.Position(symbol.end_row, symbol.end_column),
        )
        symbols.append(
            types.DocumentSymbol(
                name=symbol.path,
                kind=types.SymbolKind.Field,
                range=symbol_range,
                selection_range=symbol_range,
            )
        )
    return symbols


async def _on_did_open(ls: PynixLanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    await _sync_document(ls, params.text_document.uri)


async def _on_did_change(ls: PynixLanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    await _sync_document(ls, params.text_document.uri)


async def _on_did_close(ls: PynixLanguageServer, params: types.DidCloseTextDocumentParams) -> None:
    context = ls.contexts.pop(params.text_document.uri, None)
    if context is not None:
        await context.close()


def _on_document_symbol(ls: PynixLanguageServer, params: types.DocumentSymbolParams) -> list[types.DocumentSymbol]:
    return _document_symbols(ls, params.text_document.uri)


async def _on_shutdown(ls: PynixLanguageServer, *_args: object) -> None:
    await ls.aclose()


def create_server() -> PynixLanguageServer:
    """Build a pynix language server with all v1 features registered."""
    server = PynixLanguageServer()
    server.feature(types.TEXT_DOCUMENT_DID_OPEN)(_on_did_open)
    server.feature(types.TEXT_DOCUMENT_DID_CHANGE)(_on_did_change)
    server.feature(types.TEXT_DOCUMENT_DID_CLOSE)(_on_did_close)
    server.feature(types.TEXT_DOCUMENT_HOVER)(_hover)
    server.feature(types.TEXT_DOCUMENT_COMPLETION)(_completion)
    server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)(_on_document_symbol)
    server.feature(types.SHUTDOWN)(_on_shutdown)
    return server
