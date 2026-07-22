"""A small, backend-agnostic action language for driving LSP edit/hover/completion scenarios.

Pairs with ``lsp_markers.py``: a ``Scenario`` starts from a fixture's real
source text (with its ``# LS...`` marker comments already parsed into named
positions/ranges) and plays back a list of ``Action``s -- move the cursor,
select a marked range, type, delete, then assert on a hover or completion
result -- applying each edit as a real incremental ``textDocument/didChange``
(same wire shape a real editor sends), not a whole-document replace.

The point of the ``LspDriver`` split is that the exact same ``Scenario`` can
run against different backends without being rewritten per backend:

- ``InProcessDriver`` -- calls ``pynix._lsp._handlers``' handler functions
  directly against a real ``PynixLanguageServer`` (no wire protocol at all,
  fastest, but doesn't catch bugs that only show up in real JSON-RPC framing).
- A wire-protocol driver (built on ``pytest_lsp.LanguageClient``, whether
  connected to an in-memory or a real subprocess server) speaks genuine
  JSON-RPC, catching bugs the in-process driver structurally cannot.

Only one document is ever open per ``Scenario`` -- deliberately: the user
confirmed cross-file edit interaction is out of scope for this test
infrastructure, to keep marker-shifting and multi-document sync off the
table entirely.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from lsprotocol import types

from tests.support.lsp_markers import Marker, parse_markers

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class GoTo:
    """Move the cursor to a ``LSPOINT``/``LSSTART``/``LSLINE`` marker's start, clearing any selection."""

    marker: str


@dataclass(frozen=True)
class Select:
    """Select a ``LSSTART``/``LSEND`` (or ``LSLINE``) marker's range; cursor lands at its end."""

    marker: str


@dataclass(frozen=True)
class Type:
    """Insert text at the cursor, or replace the current selection if one is active."""

    text: str


@dataclass(frozen=True)
class Delete:
    """Remove the current selection. Requires a preceding ``Select``."""


@dataclass(frozen=True)
class InsertAfterCursor:
    """Insert text immediately after the cursor without moving it.

    Models what a real editor's bracket/quote auto-pairing does: typing an
    opening ``"`` inserts a matching closing ``"`` to the right of the
    cursor, but the cursor itself stays put between the two quotes. Useful
    for building an already-closed string literal while keeping the cursor
    positioned inside it, as ``Type`` alone cannot (it always ends with the
    cursor at the end of the inserted text).
    """

    text: str


@dataclass(frozen=True)
class ExpectHover:
    """Hover at the cursor and assert *contains* appears in the rendered Markdown."""

    contains: str


@dataclass(frozen=True)
class ExpectCompletion:
    """Complete at the cursor and assert on the returned labels.

    Subset match (`labels` must all be present, extras are ignored) unless
    `exact=True`, which requires the label set to match precisely.
    """

    labels: frozenset[str]
    exact: bool = False


Action = GoTo | Select | Type | Delete | InsertAfterCursor | ExpectHover | ExpectCompletion


class LspDriver(Protocol):
    """Backend seam: anything that can open/edit a document and answer hover/completion."""

    async def open(self, uri: str, text: str) -> None: ...

    async def edit(self, uri: str, edit_range: types.Range, text: str) -> None: ...

    async def hover(self, uri: str, position: types.Position) -> types.Hover | None: ...

    async def complete(
        self, uri: str, position: types.Position
    ) -> types.CompletionList | Sequence[types.CompletionItem] | None: ...


def _apply_text_edit(source: str, edit_range: types.Range, replacement: str) -> str:
    """Apply one incremental edit to *source*, mirroring pygls' own ``TextDocument._apply_incremental_change``.

    Kept in lockstep with pygls' algorithm deliberately: this is the same
    transform the real server will apply to its own copy of the document, so
    the scenario's local mirror of the text never drifts from what the
    server actually sees.
    """
    lines = source.splitlines(keepends=True)
    start_line, start_col = edit_range.start.line, edit_range.start.character
    end_line, end_col = edit_range.end.line, edit_range.end.character

    if start_line == len(lines):
        return source + replacement

    new = io.StringIO()
    for i, line in enumerate(lines):
        if i < start_line:
            new.write(line)
            continue
        if i > end_line:
            new.write(line)
            continue
        if i == start_line:
            new.write(line[:start_col])
            new.write(replacement)
        if i == end_line:
            new.write(line[end_col:])
    return new.getvalue()


def _end_position(start: types.Position, text: str) -> types.Position:
    """Where the cursor lands after inserting *text* starting at *start*."""
    if "\n" not in text:
        return types.Position(start.line, start.character + len(text))
    added_lines = text.count("\n")
    last_line_len = len(text.rsplit("\n", 1)[1])
    return types.Position(start.line + added_lines, last_line_len)


def _shift_position(position: types.Position, edit_range: types.Range, replacement: str) -> types.Position:
    """Translate *position* across an edit, the same way a real incremental-sync client must.

    A position at or before the edit's start is unaffected. One strictly
    inside the replaced span collapses to the edit's end (that text is
    gone). One after the edit shifts by the edit's net line/column delta.
    """
    start, end = edit_range.start, edit_range.end
    if (position.line, position.character) <= (start.line, start.character):
        return position
    if (position.line, position.character) < (end.line, end.character):
        position = end
    added_lines = replacement.count("\n")
    if position.line > end.line:
        return types.Position(position.line + added_lines - (end.line - start.line), position.character)
    if position.line == end.line:
        tail = position.character - end.character
        if added_lines == 0:
            return types.Position(start.line, start.character + len(replacement) + tail)
        last_line_len = len(replacement.rsplit("\n", 1)[1])
        return types.Position(start.line + added_lines, last_line_len + tail)
    return position


def _shift_range(range_: types.Range, edit_range: types.Range, replacement: str) -> types.Range:
    return types.Range(
        start=_shift_position(range_.start, edit_range, replacement),
        end=_shift_position(range_.end, edit_range, replacement),
    )


class Scenario:
    """A fixture's source plus a sequence of ``Action``s to play back against an ``LspDriver``."""

    def __init__(self, uri: str, source: str, actions: Sequence[Action]) -> None:
        self.uri = uri
        self._text = source
        self._markers: dict[str, Marker] = parse_markers(source)
        self._actions = actions
        self._cursor = types.Position(0, 0)
        self._selection: types.Range | None = None

    async def run(self, driver: LspDriver) -> None:
        await driver.open(self.uri, self._text)
        for action in self._actions:
            await self._apply(driver, action)

    async def _apply(self, driver: LspDriver, action: Action) -> None:
        match action:
            case GoTo(marker):
                self._cursor = self._markers[marker].range.start
                self._selection = None
            case Select(marker):
                self._selection = self._markers[marker].range
                self._cursor = self._selection.end
            case Type(text):
                edit_range = self._selection if self._selection is not None else types.Range(self._cursor, self._cursor)
                await self._edit(driver, edit_range, text)
            case Delete():
                if self._selection is None:
                    raise ValueError("Delete() requires a preceding Select(...)")
                await self._edit(driver, self._selection, "")
            case InsertAfterCursor(text):
                cursor = self._cursor
                await self._edit(driver, types.Range(cursor, cursor), text)
                self._cursor = cursor
            case ExpectHover(contains):
                hover = await driver.hover(self.uri, self._cursor)
                if hover is None:
                    raise AssertionError("expected a hover result, got None")
                if not isinstance(hover.contents, types.MarkupContent):
                    raise TypeError(f"expected MarkupContent, got {type(hover.contents)}")
                if contains not in hover.contents.value:
                    raise AssertionError(f"{contains!r} not in hover contents: {hover.contents.value!r}")
            case ExpectCompletion(labels, exact):
                completion = await driver.complete(self.uri, self._cursor)
                if completion is None:
                    raise AssertionError("expected a completion result, got None")
                items = completion.items if isinstance(completion, types.CompletionList) else completion
                actual = {item.label for item in items}
                if exact:
                    if actual != set(labels):
                        raise AssertionError(f"expected exactly {set(labels)}, got {actual}")
                elif not set(labels) <= actual:
                    raise AssertionError(f"expected at least {set(labels)}, got {actual}")

    async def _edit(self, driver: LspDriver, edit_range: types.Range, text: str) -> None:
        new_cursor = _end_position(edit_range.start, text)
        await driver.edit(self.uri, edit_range, text)
        self._text = _apply_text_edit(self._text, edit_range, text)
        self._markers = {
            name: Marker(name, _shift_range(marker.range, edit_range, text)) for name, marker in self._markers.items()
        }
        self._cursor = new_cursor
        self._selection = None
