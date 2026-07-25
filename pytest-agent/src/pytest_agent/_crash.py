"""Structured failure data: what blew up, where, and in whose code.

Extracted from pytest's own report objects at record time rather than
re-parsed out of the ``.log`` files afterwards -- pytest already has the
exception message and the traceback's file locations as data, and reading
them straight from the report is both cheaper and far less brittle than
regexing a rendered traceback back apart.

The one thing here that *is* text processing is :func:`normalize_message`,
which exists purely so that N failures sharing one root cause collapse into
one digest group even when the message embeds a per-test temp path, a store
hash, or an address.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

# How many innermost traceback frames to keep per failure. Deep recursion
# (or a deeply nested fixture chain) can produce hundreds; the frames close
# to the crash are the ones that identify the bug, and index.jsonl stays a
# file an agent can read whole.
MAX_FRAMES = 20

# "FileNotFoundError: /nix/store/...", "ValueError: bad thing" -- but not
# "assert 1 == 2", which has no type prefix at all.
_EXC_TYPE_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt|Failed|Skipped)?)\s*:\s*(.*)$")

# pytest renders exception lines in a traceback with a leading "E   ".
_EXCEPTION_LINE_RE = re.compile(r"^E\s+(.*)$")

_VENDORED_DIR_PARTS = frozenset({"site-packages", "dist-packages"})

# Applied in order: the nix-store rule has to run before the bare-digits rule,
# which would otherwise chew through the store hash it is trying to match.
_NOISE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/nix/store/[a-z0-9]{32}-"), "/nix/store/<hash>-"),
    (re.compile(r"/pytest-of-[^/\s]+/pytest-\d+"), "/pytest-of-<user>/pytest-<n>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"\d+"), "<n>"),
)


def normalize_message(message: str) -> str:
    """Collapse the per-run-varying parts of an exception message.

    Grouping key only -- callers keep one raw message per group so the agent
    still sees a real path/value rather than a message full of placeholders.
    """
    normalized = message.strip()
    for pattern, replacement in _NOISE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return re.sub(r"\s+", " ", normalized)


def split_exception_type(message: str) -> str | None:
    """The exception type at the head of *message*, if it has one."""
    match = _EXC_TYPE_RE.match(message)
    return match.group(1) if match is not None else None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _exception_line_from_text(longreprtext: str) -> str:
    """Last ``E   ...`` line of a rendered traceback.

    Fallback for reprs that carry no ``reprcrash`` at all -- notably
    ``CollectErrorRepr``, which pytest uses for some collection failures and
    which only ever exposes rendered text.
    """
    found = ""
    for line in longreprtext.splitlines():
        match = _EXCEPTION_LINE_RE.match(line.strip())
        if match is not None and match.group(1).strip():
            found = match.group(1).strip()
    return found


def _absolute(raw_path: str, rootpath: Path) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else rootpath / path


def _display(raw_path: str, rootpath: Path) -> str:
    """Render a traceback file location relative to rootdir when it is inside it."""
    absolute = _absolute(raw_path, rootpath)
    if absolute.is_relative_to(rootpath):
        return str(absolute.relative_to(rootpath))
    return str(absolute)


def _is_first_party(raw_path: str, rootpath: Path) -> bool:
    """Whether a frame is in the code under test rather than a dependency.

    Both halves matter: under rootdir excludes stdlib and system
    site-packages, and the vendored-directory check excludes a ``.venv``
    (or ``result/lib/python3.x/site-packages``) that happens to live *inside*
    rootdir, which the first check alone would happily call first-party.
    """
    absolute = _absolute(raw_path, rootpath)
    if not absolute.is_relative_to(rootpath):
        return False
    return not _VENDORED_DIR_PARTS.intersection(absolute.parts)


def crash_from_report(report: pytest.TestReport | pytest.CollectReport, rootpath: Path) -> dict[str, Any] | None:
    """The crash line ("what failed") for one report, or None if it has none.

    Duck-typed on purpose: ``report.longrepr`` is a union of several
    unrelated shapes (an ``ExceptionChainRepr`` for a normal failure, a plain
    ``(path, lineno, reason)`` tuple for a skip, a ``CollectErrorRepr`` for
    some collection failures, a bare string for others), and only some of
    them carry ``reprcrash``.
    """
    reprcrash = getattr(report.longrepr, "reprcrash", None)
    message = ""
    path: str | None = None
    lineno: int | None = None
    if reprcrash is not None:
        message = _first_line(str(getattr(reprcrash, "message", "") or ""))
        raw_path = getattr(reprcrash, "path", None)
        if raw_path:
            path = _display(str(raw_path), rootpath)
        raw_lineno = getattr(reprcrash, "lineno", None)
        if isinstance(raw_lineno, int):
            lineno = raw_lineno
    if not message:
        message = _exception_line_from_text(report.longreprtext)
    if not message:
        return None
    return {
        "exc_type": split_exception_type(message),
        "message": message,
        "path": path,
        "lineno": lineno,
    }


def frames_from_report(
    report: pytest.TestReport | pytest.CollectReport,
    rootpath: Path,
) -> list[dict[str, Any]]:
    """The traceback's file locations, innermost last, each tagged first-party or not."""
    reprtraceback = getattr(report.longrepr, "reprtraceback", None)
    entries = getattr(reprtraceback, "reprentries", None)
    if not entries:
        return []
    frames: list[dict[str, Any]] = []
    for entry in entries:
        # ReprEntryNative (used for --tb=native) has no file location at all.
        location = getattr(entry, "reprfileloc", None)
        raw_path = getattr(location, "path", None) if location is not None else None
        if not raw_path:
            continue
        raw_lineno = getattr(location, "lineno", None)
        frames.append(
            {
                "path": _display(str(raw_path), rootpath),
                "lineno": raw_lineno if isinstance(raw_lineno, int) else None,
                "first_party": _is_first_party(str(raw_path), rootpath),
            },
        )
    return frames[-MAX_FRAMES:]
