"""pygls request handlers for pynix's Nix language server."""

from __future__ import annotations

import logging
import sys
import threading
from typing import TYPE_CHECKING

from lsprotocol import types
from pygls.io_ import StdinAsyncReader, StdoutWriter, run_async
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

import nanopynix
from nanopynix.exceptions import NixError
from pynix._lsp._context import FileContext, parse_directives, resolve_root_path
from pynix._lsp._dialects import DIALECTS
from pynix._lsp._render import render_value
from pynix._lsp._syntax import completion_target_at, identifier_path_at, parse_errors, top_level_symbols

if TYPE_CHECKING:
    from pynix._lsp._syntax import ParseErrorRange

_SERVER_NAME = "pynix-lsp"
_SERVER_VERSION = "0.1.0"
_logger = logging.getLogger(__name__)


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
        self.diagnostics: dict[str, list[types.Diagnostic]] = {}

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

    async def start_io_async(self) -> None:
        """Run pygls' stdio message loop on the caller's own event loop.

        pygls' own ``start_io()`` is synchronous and always wraps its message
        loop in a fresh ``asyncio.run(...)``, which cannot nest inside the
        loop clypi already runs this command under. The loop itself
        (``pygls.io_.run_async``) is a plain coroutine with no such
        requirement -- it dispatches its own blocking stdin reads via
        ``loop.run_in_executor`` -- so driving it directly here avoids a
        redundant second thread/event loop for the server's whole lifetime.
        """
        stop_event = threading.Event()
        reader = StdinAsyncReader(sys.stdin.buffer, self.thread_pool)
        writer = StdoutWriter(sys.stdout.buffer)
        self.protocol.set_writer(writer)
        try:
            await run_async(
                stop_event=stop_event,
                reader=reader,
                protocol=self.protocol,
                logger=_logger,
                error_handler=self.report_server_error,
            )
        except BrokenPipeError:
            _logger.exception("connection to the client was lost")
        finally:
            self.shutdown()


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


def _context_error_diagnostic(error: NixError, line: int) -> types.Diagnostic:
    position = types.Position(line, 0)
    return types.Diagnostic(
        range=types.Range(start=position, end=position),
        message=f"pynix-lsp context failed to evaluate: {error.msg_without_ansi}",
        severity=types.DiagnosticSeverity.Error,
        source=_SERVER_NAME,
    )


async def _sync_document(ls: PynixLanguageServer, uri: str) -> None:
    """Reconcile one document's context against its current header directives.

    Only (re)evaluates the context expressions when the directive text
    itself changed -- routine edits to the rest of the file just get fresh
    parse diagnostics, matching nixd's own "assume an evaluated value
    doesn't change until told otherwise" caching stance.
    """
    document = ls.workspace.get_text_document(uri)
    source = document.source
    directives = parse_directives(source)
    context = ls.contexts.get(uri)

    if not directives:
        if context is not None:
            await context.close()
            del ls.contexts[uri]
        context = None
    elif context is None or context.directives != directives:
        file_path = to_fs_path(uri)
        file_dir = file_path.rsplit("/", 1)[0] if file_path and "/" in file_path else "."
        session, store = await ls.ensure_nix()
        context = FileContext(session, store, directives, file_dir)
        await context.reload()
        for dialect in DIALECTS:
            await dialect.derive_roots(context)
        ls.contexts[uri] = context

    diagnostics = [_parse_error_diagnostic(error) for error in parse_errors(source)]
    if context is not None:
        directives_by_name = {directive.name: directive for directive in context.directives}
        diagnostics.extend(
            _context_error_diagnostic(error, directives_by_name[name].line)
            for name, error in context.errors.items()
        )
        for dialect in DIALECTS:
            dialect_diagnostics = await dialect.diagnostics(context, source)
            if dialect_diagnostics is not None:
                diagnostics.extend(dialect_diagnostics)
    ls.diagnostics[uri] = diagnostics
    ls.text_document_publish_diagnostics(types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics))


async def _hover(ls: PynixLanguageServer, params: types.HoverParams) -> types.Hover | None:
    """Dialects get first refusal, since some (e.g. terranix's schema lookup) must override what a plain root walk would show.

    A dialect returns non-None only for paths it specifically recognizes as
    its own (see e.g. ``ModuleSystemDialect``'s "already a bound root, defer"
    guard); the generic ``resolve_root_path`` walk is the fallback for
    everything else.
    """
    uri = params.text_document.uri
    context = ls.contexts.get(uri)
    if context is None:
        return None
    document = ls.workspace.get_text_document(uri)
    source = document.source
    byte_offset = _byte_offset(source, params.position, ls, uri)
    for dialect in DIALECTS:
        rendered = await dialect.hover(context, source, byte_offset, DIALECTS)
        if rendered is not None:
            return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=rendered))
    path = identifier_path_at(source, byte_offset)
    if path is None:
        return None
    value = await resolve_root_path(context, path)
    if value is None:
        return None
    try:
        rendered = await render_value(value, DIALECTS)
    except NixError:
        return None
    return types.Hover(contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value=rendered))


async def _completion(ls: PynixLanguageServer, params: types.CompletionParams) -> types.CompletionList | None:
    """Dialects get first refusal; see ``_hover``'s docstring for why."""
    uri = params.text_document.uri
    context = ls.contexts.get(uri)
    if context is None:
        return None
    document = ls.workspace.get_text_document(uri)
    source = document.source
    byte_offset = _byte_offset(source, params.position, ls, uri)
    for dialect in DIALECTS:
        items = await dialect.complete(context, source, byte_offset, DIALECTS)
        if items is not None:
            return types.CompletionList(is_incomplete=False, items=items)
    target = completion_target_at(source, byte_offset)
    if target is None:
        return None
    prefix, partial = target
    value = await resolve_root_path(context, prefix)
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
    server.feature(types.TEXT_DOCUMENT_COMPLETION, types.CompletionOptions(trigger_characters=["."]))(_completion)
    server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)(_on_document_symbol)
    server.feature(types.SHUTDOWN)(_on_shutdown)
    return server
