"""pygls request handlers for pynix's Nix language server."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import TYPE_CHECKING

import anyio
from lsprotocol import types

# `types.PrepareRenameResult` is a PEP 563-deferred type alias whose own
# string annotation ("Range | RenameFilePlaceholder | None") beartype
# resolves against *this* module's namespace rather than lsprotocol.types',
# so `_prepare_rename`'s return hint can't be checked unless a bare `Range`
# name exists here too -- not used by name anywhere below, only re-exported
# for beartype's forward-ref lookup to find.
from lsprotocol.types import Range as Range
from pygls.io_ import StdinAsyncReader, StdoutWriter, run_async
from pygls.lsp.server import LanguageServer
from pygls.uris import from_fs_path, to_fs_path
from pygls.workspace import TextDocument

import nanopynix
from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError
from pynix_lsp._context import FileContext, SharedEvalCache, parse_directives, resolve_root_path
from pynix_lsp._dialects import DIALECTS
from pynix_lsp._module_system import resolve_module_arg
from pynix_lsp._render import render_value
from pynix_lsp._syntax import (
    completion_target_at,
    identifier_path_at,
    local_scope_at,
    parse_errors,
    path_literal_at,
    top_level_lambda_formals,
    top_level_symbols,
)

if TYPE_CHECKING or BEARTYPING:
    from pynix_lsp._syntax import ParseErrorRange

_SERVER_NAME = "pynix-lsp"
_SERVER_VERSION = "0.1.0"
_logger = logging.getLogger(__name__)


class PynixLanguageServer(LanguageServer):
    """Owns the one shared Nix evaluator worker for the whole server session.

    Each open file with a ``# pynix-lsp:`` directive gets its own
    ``FileContext`` within this one shared ``Session``/``Store`` --
    opening a new ``EvalSession`` is cheap (a dedicated thread in the
    existing worker), unlike spawning a whole new worker subprocess per
    file. ``FileContext`` itself resolves each directive's ``EvalSession``
    via ``shared_evals`` (a ``SharedEvalCache``), so two files declaring the
    byte-identical directive expression share one already-evaluated
    ``EvalSession`` rather than each paying its evaluation cost separately
    -- see ``SharedEvalCache``'s own docstring.
    """

    def __init__(self) -> None:
        super().__init__(_SERVER_NAME, _SERVER_VERSION)  # type: ignore[reportUnknownMemberType] -- pygls' LanguageServer.__init__ forwards *args/**kwargs, so pyright can't fully resolve its composed signature
        self.nix_session: nanopynix.rpc.Session | None = None
        self.store: nanopynix.rpc.Store | None = None
        self.shared_evals: SharedEvalCache | None = None
        self.contexts: dict[str, FileContext] = {}
        self.diagnostics: dict[str, list[types.Diagnostic]] = {}
        # The most recent per-file source snapshot that parsed with zero
        # ERROR/MISSING nodes, and whether the *live* snapshot currently has
        # any -- see `_sync_document`'s docstring and `_document_symbols`/
        # `_hover`'s use of these for why. Only ever read/written from
        # `_sync_document`'s call site and the handlers that consult them;
        # `_on_did_close` drops both so a closed file doesn't linger here.
        self.last_good_source: dict[str, str] = {}
        self.has_parse_errors: dict[str, bool] = {}
        # Strong references to fire-and-forget module-arg warm-up tasks (see
        # `_warm_module_args`) -- otherwise nothing keeps them alive and
        # asyncio is free to garbage-collect a still-running task.
        self.warm_tasks: set[asyncio.Task[None]] = set()

    async def ensure_nix(self) -> tuple[nanopynix.rpc.Session, nanopynix.rpc.Store, SharedEvalCache]:
        """Open the shared Session/Store/SharedEvalCache on first use.

        ``nix_session``/``store`` may already be set without going through
        this method at all (the test suite's ``lsp_server`` fixture seeds
        them directly from a shared, test-session-lifetime worker rather
        than spawning a fresh one per test) -- only missing pieces are
        created, so this must never unconditionally build a brand new
        ``nanopynix.rpc.Session`` when one is already sitting there.
        """
        if self.nix_session is None or self.store is None:
            session = nanopynix.rpc.Session(experimental_features=["flakes", "nix-command"])
            await session.open()
            store = session.store()
            await store.open()
            self.nix_session = session
            self.store = store
        if self.shared_evals is None:
            self.shared_evals = SharedEvalCache(self.nix_session, self.store)
        return self.nix_session, self.store, self.shared_evals

    async def aclose(self) -> None:
        """Close every open file context, the shared eval cache, and the shared Session/Store."""
        for context in self.contexts.values():
            await context.close()
        self.contexts.clear()
        if self.shared_evals is not None:
            await self.shared_evals.close_all()
            self.shared_evals = None
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
        loop ``pynix._impl.main.run`` already runs this command under. The loop itself
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


def _byte_offset_in(uri: str, source: str, position: types.Position) -> int:
    """Like ``_byte_offset``, but against an arbitrary *source* rather than the live workspace document.

    Used for the last-good-tree fallback: a cached snapshot's line/byte
    layout generally differs from the live document's (the edit that broke
    the live parse almost always changes some line's length), so reusing a
    byte offset computed against the live document would land on the wrong
    spot in the cached one. Building a standalone ``TextDocument`` lets the
    same (line, character) position be re-resolved against the cached
    text's own layout instead.
    """
    document = TextDocument(uri, source=source)
    char_offset = document.offset_at_position(position)
    return len(source[:char_offset].encode("utf-8"))


def _file_dir(uri: str) -> str:
    """The filesystem directory containing *uri*, or ``"."`` if it can't be resolved to a real path."""
    file_path = to_fs_path(uri)
    return file_path.rsplit("/", 1)[0] if file_path and "/" in file_path else "."


def _resolve_source(ls: PynixLanguageServer, uri: str, position: types.Position) -> tuple[str, int]:
    """Return the ``(source, byte_offset)`` pair every syntax-derived handler below should resolve *position* against.

    Prefers the live document, falling back to the last snapshot that
    parsed clean if the live document currently has parse errors anywhere
    -- see ``_sync_document``'s docstring for why (tree-sitter-nix's error
    recovery can shred real, already-complete code arbitrarily far from
    wherever the live syntax error actually is). *position* itself always
    comes from the live request; only the source text/byte offset it's
    resolved against may switch to the cached snapshot, via
    ``_byte_offset_in`` re-deriving the offset against that snapshot's own
    layout (reusing the live offset would drift whenever the edit changed a
    preceding line's length -- almost always).
    """
    document = ls.workspace.get_text_document(uri)
    source = document.source
    byte_offset = _byte_offset(source, position, ls, uri)
    if ls.has_parse_errors.get(uri, False):
        last_good = ls.last_good_source.get(uri)
        if last_good is not None:
            source = last_good
            byte_offset = _byte_offset_in(uri, last_good, position)
    return source, byte_offset


def _lsp_range(span: tuple[int, int, int, int]) -> types.Range:
    start_row, start_column, end_row, end_column = span
    return types.Range(types.Position(start_row, start_column), types.Position(end_row, end_column))


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


async def _warm_module_args(context: FileContext, source: str) -> None:
    """Best-effort background warm-up: force every module-arg this file's own top-level lambda declares.

    Resolving a module arg (e.g. ``pkgs``) means forcing ``_module.args``/
    ``_module.specialArgs`` through the module system for the first time in
    this ``EvalSession`` -- confirmed to cost several real seconds the first
    time (a complex module composition like easykubenix's can take ~8s),
    dropping to well under a second on every subsequent force once the
    underlying ``EvalState`` has cached it. Running this the moment a file's
    context is created, rather than waiting for the user's first completion
    request to pay that cost synchronously, is what actually fixes the
    "completion just doesn't seem to fire" symptom -- a real editor's
    completion popup has nowhere near an 8s patience budget.

    Can race a fast did_open -> did_close (this file's `EvalSession` may
    already be released -- and, if this was the last file sharing it,
    closed -- by the time this finishes); caught broadly and logged at
    debug level rather than left to bubble up as an unhandled task
    exception, since there's no request left waiting on this result by then.
    """
    for name in top_level_lambda_formals(source) or []:
        try:
            await resolve_module_arg(context, name)
        except Exception:
            _logger.debug("module-arg warm-up for %r failed (file likely closed mid-warm)", name, exc_info=True)


async def _sync_document(ls: PynixLanguageServer, uri: str) -> None:
    """Reconcile one document's context against its current header directives.

    Only (re)evaluates the context expressions when the directive text
    itself changed -- routine edits to the rest of the file just get fresh
    parse diagnostics, matching nixd's own "assume an evaluated value
    doesn't change until told otherwise" caching stance.

    Also records whether *this* snapshot parsed error-free, and if so caches
    it as ``ls.last_good_source[uri]`` -- tree-sitter-nix's error recovery
    can have an unbounded blast radius (an incomplete ``formals`` list left
    open mid-edit doesn't stay contained; it keeps trying to extend the
    comma-separated formal list and can shred real, already-complete
    bindings arbitrarily far downstream in the same file, not just the
    token actually being typed). ``_document_symbols``/``_hover`` fall back
    to this cached snapshot rather than showing corrupted or missing results
    for code that hasn't actually changed, at the cost of showing slightly
    stale (but structurally correct) data for the file until the in-progress
    edit closes its brace again.
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
        _session, _store, shared_evals = await ls.ensure_nix()
        context = FileContext(shared_evals, directives, _file_dir(uri))
        await context.reload()
        for dialect in DIALECTS:
            await dialect.derive_roots(context)
        ls.contexts[uri] = context
        warm_task = asyncio.create_task(_warm_module_args(context, source))
        ls.warm_tasks.add(warm_task)
        warm_task.add_done_callback(ls.warm_tasks.discard)

    parse_error_ranges = parse_errors(source)
    ls.has_parse_errors[uri] = bool(parse_error_ranges)
    if not parse_error_ranges:
        ls.last_good_source[uri] = source

    diagnostics = [_parse_error_diagnostic(error) for error in parse_error_ranges]
    if context is not None:
        directives_by_name = {directive.name: directive for directive in context.directives}
        diagnostics.extend(
            _context_error_diagnostic(error, directives_by_name[name].line) for name, error in context.errors.items()
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

    When the live document currently has parse errors anywhere, hover -- for
    both dialects and the generic walk below -- is computed against the last
    snapshot that parsed clean instead of the live text (see
    ``_sync_document``'s docstring for why: tree-sitter-nix's error recovery
    can shred real, already-complete code arbitrarily far from wherever the
    live syntax error actually is). The (line, character) position itself
    still comes from the live request; only the *source text and byte
    offset it's resolved against* switch to the cached snapshot, via
    ``_byte_offset_in`` re-deriving the offset against that snapshot's own
    layout rather than reusing the live one (which would drift whenever the
    edit changed a preceding line's length -- almost always). This is
    decided once, before dispatching to dialects, so every dialect benefits
    uniformly without needing its own fallback logic.
    """
    uri = params.text_document.uri
    context = ls.contexts.get(uri)
    if context is None:
        return None
    source, byte_offset = _resolve_source(ls, uri, params.position)
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
    """Dialects get first refusal; see ``_hover``'s docstring for why.

    Unlike hover, a dialect's *empty* completion list does not win here --
    only a genuinely non-empty one does, falling through to the next
    dialect (then the generic walk) otherwise. ``ModuleSystemDialect``'s own
    ``complete`` always resolves a bare/dot-less prefix against the whole
    ``options`` root regardless of what other dialect-specific context the
    cursor is actually in (e.g. inside a Kubernetes object body), and an
    empty-but-non-None result from that generic resolution must not block a
    later, more specific dialect (e.g. ``EasykubenixDialect``) from offering
    real completions for the exact same position.
    """
    uri = params.text_document.uri
    context = ls.contexts.get(uri)
    if context is None:
        return None
    document = ls.workspace.get_text_document(uri)
    source = document.source
    byte_offset = _byte_offset(source, params.position, ls, uri)
    for dialect in DIALECTS:
        items = await dialect.complete(context, source, byte_offset, DIALECTS)
        if items:
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


async def _path_literal_location(uri: str, path_literal: str) -> types.Location | None:
    """Resolve a static path literal (``./foo.nix``) to a real file's ``Location``, or None if it doesn't exist.

    A directory target (``imports = [ ./mymodule ];``, the common NixOS
    convention) resolves to its own ``default.nix`` when present, matching
    what ``import`` itself would actually load.
    """
    target = anyio.Path(_file_dir(uri)) / path_literal
    if await target.is_dir():
        default_nix = target / "default.nix"
        if await default_nix.exists():
            target = default_nix
    if not await target.exists():
        return None
    resolved_uri = from_fs_path(str(target))
    if resolved_uri is None:
        return None
    origin = types.Position(0, 0)
    return types.Location(uri=resolved_uri, range=types.Range(origin, origin))


async def _definition(
    ls: PynixLanguageServer,
    params: types.DefinitionParams,
) -> types.Location | list[types.Location] | None:
    """Structural lookups (path literal, local scope) first, then dialects -- mirrors ``_hover``'s dispatch order.

    Path-literal and local-scope resolution work even with no directive
    bound at all (pure syntax, same "always available" tier diagnostics
    already get); dialects only run when this file has an evaluation
    context.
    """
    uri = params.text_document.uri
    source, byte_offset = _resolve_source(ls, uri, params.position)

    path_literal = path_literal_at(source, byte_offset)
    if path_literal is not None:
        location = await _path_literal_location(uri, path_literal)
        if location is not None:
            return location

    local = local_scope_at(source, byte_offset)
    if local is not None:
        return types.Location(uri=uri, range=_lsp_range(local.definition))

    context = ls.contexts.get(uri)
    if context is None:
        return None
    for dialect in DIALECTS:
        location = await dialect.definition(context, source, byte_offset, DIALECTS)
        if location is not None:
            return location
    return None


async def _prepare_rename(
    ls: PynixLanguageServer,
    params: types.PrepareRenameParams,
) -> types.PrepareRenameResult | None:
    """Refuse (return None) unless the cursor is on a v1-renameable local binding -- see ``local_scope_at``'s docstring."""
    uri = params.text_document.uri
    source, byte_offset = _resolve_source(ls, uri, params.position)
    local = local_scope_at(source, byte_offset)
    if local is None:
        return None
    return _lsp_range(local.definition)


async def _rename(ls: PynixLanguageServer, params: types.RenameParams) -> types.WorkspaceEdit | None:
    """Rename a local lexical binding and every reference to it, all within this one document.

    Local-lexical-scope only (``let`` bindings, function formals) -- see
    ``local_scope_at``'s docstring for exactly which binding kinds qualify.
    Cross-file/semantic rename (e.g. a NixOS option and every module that
    sets it) is out of scope: unsafe without a real project graph, and this
    server's LSP surface only ever reasons about one open document.
    """
    uri = params.text_document.uri
    source, byte_offset = _resolve_source(ls, uri, params.position)
    local = local_scope_at(source, byte_offset)
    if local is None:
        return None
    spans = [local.definition, *local.references]
    edits = [types.TextEdit(range=_lsp_range(span), new_text=params.new_name) for span in spans]
    return types.WorkspaceEdit(changes={uri: edits})


async def _document_highlight(
    ls: PynixLanguageServer,
    params: types.DocumentHighlightParams,
) -> list[types.DocumentHighlight] | None:
    uri = params.text_document.uri
    source, byte_offset = _resolve_source(ls, uri, params.position)
    local = local_scope_at(source, byte_offset)
    if local is None:
        return None
    highlights = [types.DocumentHighlight(range=_lsp_range(local.definition), kind=types.DocumentHighlightKind.Write)]
    highlights.extend(
        types.DocumentHighlight(range=_lsp_range(span), kind=types.DocumentHighlightKind.Read)
        for span in local.references
    )
    return highlights


async def _references(ls: PynixLanguageServer, params: types.ReferenceParams) -> list[types.Location] | None:
    uri = params.text_document.uri
    source, byte_offset = _resolve_source(ls, uri, params.position)
    local = local_scope_at(source, byte_offset)
    if local is None:
        return None
    spans = list(local.references)
    if params.context.include_declaration:
        spans.append(local.definition)
    return [types.Location(uri=uri, range=_lsp_range(span)) for span in spans]


def _document_symbols(ls: PynixLanguageServer, uri: str) -> list[types.DocumentSymbol]:
    document = ls.workspace.get_text_document(uri)
    source = document.source
    if ls.has_parse_errors.get(uri, False):
        # Whole-file outline has no cursor position to translate, unlike
        # `_hover` -- always safe to prefer the last clean snapshot outright
        # rather than only on a live miss. See `_sync_document`'s docstring.
        source = ls.last_good_source.get(uri, source)
    symbols: list[types.DocumentSymbol] = []
    for symbol in top_level_symbols(source):
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
            ),
        )
    return symbols


async def _on_did_open(ls: PynixLanguageServer, params: types.DidOpenTextDocumentParams) -> None:
    await _sync_document(ls, params.text_document.uri)


async def _on_did_change(ls: PynixLanguageServer, params: types.DidChangeTextDocumentParams) -> None:
    await _sync_document(ls, params.text_document.uri)


async def _on_did_close(ls: PynixLanguageServer, params: types.DidCloseTextDocumentParams) -> None:
    uri = params.text_document.uri
    context = ls.contexts.pop(uri, None)
    if context is not None:
        await context.close()
    ls.last_good_source.pop(uri, None)
    ls.has_parse_errors.pop(uri, None)


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
    server.feature(types.TEXT_DOCUMENT_DEFINITION)(_definition)
    server.feature(types.TEXT_DOCUMENT_PREPARE_RENAME)(_prepare_rename)
    server.feature(types.TEXT_DOCUMENT_RENAME)(_rename)
    server.feature(types.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT)(_document_highlight)
    server.feature(types.TEXT_DOCUMENT_REFERENCES)(_references)
    server.feature(types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)(_on_document_symbol)
    server.feature(types.SHUTDOWN)(_on_shutdown)
    return server
