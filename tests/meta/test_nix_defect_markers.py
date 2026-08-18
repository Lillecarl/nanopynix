"""Each marker of a divergence has the shape that section 5b asks for.

Matching the bytes of `nix-daemon` is a measure, and it is not the goal. A
difference between the two recordings of the parity run is how a divergence
becomes visible, and every divergence then gets a verdict. A comment that
reads as if Nix is the specification hides the difference between "pynixd does
this because it is right" and "pynixd does this because the parity run
compares the bytes".

Two tags carry the two verdicts, and each one names its own tracking issue:

- `NIX-DEFECT (#191):` marks a place where Nix is wrong. pynixd copies the
  defect, or pynixd answers correctly, and part 4 says which.
- `NIX-DEVIATION (#206):` marks a place where Nix is right, or where neither
  answer is wrong, and pynixd answers differently on purpose.

Do not write `NIX-DEFECT` for a place where Nix is right. The tag then states
a defect that nobody found.

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

TRACKING_ISSUE: dict[str, int] = {
    "NIX-DEFECT": 191,
    "NIX-DEVIATION": 206,
}
"""The tracking issue of each tag. A tag with the wrong number fails."""

MARKER = re.compile(rf"(?P<tag>{'|'.join(TRACKING_ISSUE)})(?P<issue>[^:]*):")
"""Every spelling of every tag, so a wrong one fails rather than hides."""


def _correct(tag: str) -> str:
    """The one spelling that the convention accepts, for this tag."""
    return f"{tag} (#{TRACKING_ISSUE[tag]}):"


NIX_SOURCE = re.compile(r"`[\w./-]+\.(?:cc|hh)(?::\d+(?:-\d+)?)?`")
"""A file of Nix, and the line if the marker gives one, in back quotes."""

SEARCH_ROOT = REPO_ROOT / "pynixd"

# The paragraph of a marker ends at a blank comment line, at a blank line, or
# at the end of the block. 40 lines is far past the longest one written.
PARAGRAPH_LINES = 40


def _sources() -> list[Path]:
    return sorted(p for p in SEARCH_ROOT.rglob("*.py") if "/tests/" not in p.as_posix())


def _markers() -> list[tuple[Path, int, str, str]]:
    """Each marker, as the file, the 1-based line, the tag, and the paragraph."""
    found: list[tuple[Path, int, str, str]] = []
    for path in _sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = MARKER.search(line)
            if match is None:
                continue
            paragraph = _paragraph(lines, index)
            found.append((path, index + 1, match.group("tag"), paragraph))
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
    assert _markers(), f"no divergence marker under {SEARCH_ROOT}; the regex or the root is wrong"


@pytest.mark.parametrize(("path", "line", "tag", "paragraph"), _markers(), ids=lambda v: str(v)[-40:])
def test_the_marker_names_the_tracking_issue(path: Path, line: int, tag: str, paragraph: str) -> None:
    """Each tag has one tracking issue, and the other tag's number is a fault."""
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    correct = _correct(tag)
    assert correct in paragraph, f"{where}: write the tag as `{correct}`, and not as it reads now"


@pytest.mark.parametrize(("path", "line", "tag", "paragraph"), _markers(), ids=lambda v: str(v)[-40:])
def test_the_marker_names_a_place_in_the_source_of_nix(path: Path, line: int, tag: str, paragraph: str) -> None:
    """A reader has to be able to check the claim against Nix."""
    where = f"{path.relative_to(REPO_ROOT)}:{line}"
    assert NIX_SOURCE.search(paragraph), (
        f"{where}: a `{tag}` marker names the file of Nix that holds the mechanism, "
        "in back quotes, for example `goal.cc:214`"
    )
