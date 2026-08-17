"""Each `NIX-DEFECT` marker has the shape that issue #191 asks for.

pynixd matches the bytes of `nix-daemon` on the wire, so it copies decisions
of Nix that are wrong. Nix is not perfect: C++ limits what its authors can do
easily, and Python does not carry the same limits. A comment that reads as if
Nix is the specification hides the difference between "pynixd does this
because it is right" and "pynixd does this because the parity run compares the
bytes".

The convention is a literal tag, `NIX-DEFECT (#191):`, in front of the
paragraph that gives four parts:

1. the mechanism in Nix, with the file and the line;
2. what the mechanism gets wrong;
3. what pynixd could do instead;
4. why pynixd still copies it, or how pynixd already deviates.

Two things decay, and neither fails a build:

- A marker names another issue, or no issue, so the tracking issue loses it.
- A marker names no place in the source of Nix, so a reader cannot check the
  claim, and the claim becomes folklore.

This test finds every marker and reads the two parts that a machine can read.
It does not judge the prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support.suite_roots import REPO_ROOT

TRACKING_ISSUE = 191

MARKER = re.compile(r"NIX-DEFECT(?P<issue>[^:]*):")
"""Every spelling of the tag, so a wrong one fails rather than hides."""

CORRECT = f"NIX-DEFECT (#{TRACKING_ISSUE}):"

NIX_SOURCE = re.compile(r"`[\w./-]+\.(?:cc|hh)(?::\d+(?:-\d+)?)?`")
"""A file of Nix, and the line if the marker gives one, in back quotes."""

SEARCH_ROOT = REPO_ROOT / "pynixd"

# The paragraph of a marker ends at a blank comment line, at a blank line, or
# at the end of the block. 40 lines is far past the longest one written.
PARAGRAPH_LINES = 40


def _sources() -> list[Path]:
    return sorted(p for p in SEARCH_ROOT.rglob("*.py") if "/tests/" not in p.as_posix())


def _markers() -> list[tuple[Path, int, str]]:
    """Each marker, as the file, the 1-based line, and the paragraph after it."""
    found: list[tuple[Path, int, str]] = []
    for path in _sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not MARKER.search(line):
                continue
            paragraph = _paragraph(lines, index)
            found.append((path, index + 1, paragraph))
    return found


def _paragraph(lines: list[str], start: int) -> str:
    """The marker line and the lines under it, to the first empty one."""
    collected = [lines[start]]
    for line in lines[start + 1 : start + PARAGRAPH_LINES]:
        stripped = line.strip().removeprefix("#").strip()
        if not stripped:
            break
        collected.append(line)
    return "\n".join(collected)


def test_the_repository_holds_at_least_one_marker() -> None:
    """A test that finds nothing proves nothing, so say when the sweep is empty."""
    assert _markers(), f"no NIX-DEFECT marker under {SEARCH_ROOT}; the regex or the root is wrong"


@pytest.mark.parametrize(("path", "line", "paragraph"), _markers(), ids=lambda v: str(v)[-40:])
def test_the_marker_names_the_tracking_issue(path: Path, line: int, paragraph: str) -> None:
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    assert CORRECT in paragraph, f"{where}: write the tag as `{CORRECT}`, and not as it reads now"


@pytest.mark.parametrize(("path", "line", "paragraph"), _markers(), ids=lambda v: str(v)[-40:])
def test_the_marker_names_a_place_in_the_source_of_nix(path: Path, line: int, paragraph: str) -> None:
    """A reader has to be able to check the claim against Nix."""
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    assert NIX_SOURCE.search(paragraph), (
        f"{where}: name the file of Nix that holds the mechanism, in back quotes, for example `goal.cc:214`"
    )
