"""Comment-based position markers for LSP scenario tests.

Nix has no mid-line comments, so a marker can't sit next to the code it
points at -- it has to live on its own comment line immediately above the
code line it annotates (down-only; there is no "points at the line above"
form). A marker is a ``LS<ROLE><id>`` tag with a ``v`` caret glued to either
side of it -- the caret's own column (wherever you actually put it) is the
position it names, not anything the parser infers from indentation. Putting
the caret on whichever side leaves room is what lets two markers sit close
together on one comment line (e.g. a tight ``LSSTART``/``LSEND`` pair
bracketing a two-character selection)::

    # LSPOINT1v"r"
    content = lib.tfRef "random_password.example.result";
    # LSSTART2v"r"                                    vLSEND2"t"
    content2 = lib.tfRef "random_password.example.result";

    # LSLINE3
    output.greeting_path.value = lib.tfRef "local_file.greeting.filename";

Roles:

- ``LSPOINT<id>`` -- a single cursor position on the next code line, at the
  caret's column.
- ``LSSTART<id>``/``LSEND<id>`` -- a selection range on the next code line,
  from one caret to the other. Both carets must appear on the same comment
  line, sharing the same id.
- ``LSLINE<id>`` -- the entire next code line, no caret needed (used to mark
  "select this whole line" for a clear-and-retype scenario).

A caret may carry an inline self-check, e.g. ``LSPOINT1v"r"``, asserting the
next line's text at that exact column starts with ``"r"``. This is what
keeps the scheme honest: hand-aligning a caret to a column is still manual,
but a stale caret (one that no longer lines up after an edit elsewhere in
the file) fails loudly at parse time instead of silently pointing at the
wrong character.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lsprotocol import types

_MARKER_RE = re.compile(
    r'(?P<pre>v)?LS(?P<role>POINT|START|END)(?P<id>[A-Za-z0-9_]+)(?(pre)|v)(?:"(?P<check>[^"]*)")?',
)
_LINE_MARKER_RE = re.compile(r"LSLINE(?P<id>[A-Za-z0-9_]+)")


@dataclass(frozen=True)
class Marker:
    """A named position (``range.start == range.end``) or range on some code line."""

    id: str
    range: types.Range


def _target_line(lines: list[str], comment_line_number: int) -> tuple[int, str]:
    """Return the next non-blank line below *comment_line_number*, skipping blank spacer lines.

    A marker comment and its target don't have to be strictly adjacent -- a
    blank line in between is allowed purely for readability -- but the
    target must be real code, not another comment (that's almost certainly a
    misplaced marker, not something to silently resolve further).
    """
    target_number = comment_line_number + 1
    while target_number < len(lines) and not lines[target_number].strip():
        target_number += 1
    if target_number >= len(lines):
        raise ValueError(f"line {comment_line_number + 1}: marker has no target line below it")
    target_line = lines[target_number]
    if target_line.lstrip().startswith("#"):
        raise ValueError(
            f"line {comment_line_number + 1}: marker's target line {target_number + 1} is itself a comment"
        )
    return target_number, target_line


def _check_column(target_line: str, column: int, check: str | None, *, context: str) -> None:
    if check is None:
        return
    actual = target_line[column : column + len(check)]
    if actual != check:
        raise ValueError(
            f"{context}: expected {check!r} at column {column} of {target_line!r}, found {actual!r} "
            "-- the marker's caret column no longer lines up with its target text",
        )


def parse_markers(source: str) -> dict[str, Marker]:  # noqa: C901 -- tracked complexity/arg-count debt, see TODO.md
    """Parse every ``# LS...`` marker comment in *source* into a name -> ``Marker`` map.

    Raises ``ValueError`` on a malformed scenario file: a marker with no
    target line, a caret whose self-check doesn't match, a duplicate id, or
    an ``LSSTART``/``LSEND`` whose partner is missing from the same comment
    line.
    """
    lines = source.splitlines()
    markers: dict[str, Marker] = {}

    for line_number, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            continue

        line_marker_match = _LINE_MARKER_RE.search(line)
        if line_marker_match is not None:
            marker_id = line_marker_match.group("id")
            if marker_id in markers:
                raise ValueError(f"line {line_number + 1}: duplicate marker id {marker_id!r}")
            target_number, target_line = _target_line(lines, line_number)
            markers[marker_id] = Marker(
                marker_id,
                types.Range(
                    start=types.Position(target_number, 0),
                    end=types.Position(target_number, len(target_line)),
                ),
            )
            continue

        matches = list(_MARKER_RE.finditer(line))
        if not matches:
            continue
        target_number, target_line = _target_line(lines, line_number)

        # START/END halves are only ever paired against another half found
        # on this SAME comment line -- a fresh dict per line, not carried
        # across iterations, so an id reused (by mistake) on unrelated lines
        # can never silently cross-pair.
        line_ranges: dict[str, dict[str, int]] = {}

        for match in matches:
            role = match.group("role")
            marker_id = match.group("id")
            caret_column = match.start("pre") if match.group("pre") else match.end("id")
            _check_column(
                target_line,
                caret_column,
                match.group("check"),
                context=f"line {line_number + 1} marker LS{role}{marker_id}",
            )

            if role == "POINT":
                if marker_id in markers:
                    raise ValueError(f"line {line_number + 1}: duplicate marker id {marker_id!r}")
                position = types.Position(target_number, caret_column)
                markers[marker_id] = Marker(marker_id, types.Range(start=position, end=position))
                continue

            half = line_ranges.setdefault(marker_id, {})
            if role in half:
                raise ValueError(f"line {line_number + 1}: duplicate LS{role}{marker_id} on the same line")
            half[role] = caret_column

        for marker_id, half in line_ranges.items():
            if "START" not in half or "END" not in half:
                missing = "END" if "START" in half else "START"
                raise ValueError(f"line {line_number + 1}: LS{marker_id} is missing its LS{missing}{marker_id} partner")
            if marker_id in markers:
                raise ValueError(f"line {line_number + 1}: duplicate marker id {marker_id!r}")
            markers[marker_id] = Marker(
                marker_id,
                types.Range(
                    start=types.Position(target_number, half["START"]),
                    end=types.Position(target_number, half["END"]),
                ),
            )

    return markers
