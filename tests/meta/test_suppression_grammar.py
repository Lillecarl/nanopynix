"""Every lint/type suppression in the tree must say why it is there.

CLAUDE.md's rule is ``# noqa: RULE -- reason`` / ``# type: ignore[rule] --
reason``. Ruff's PGH003/PGH004 enforce that codes are named; nothing enforces
the justification, and neither rule covers ``# pyright: ignore[...]`` at all.
The scanner in ``tests/support/suppressions.py`` is that missing half, and this
is where it runs -- pytest is the only check CI actually executes, so a
conformance test is the only form this gate can take and still be enforced.

The unit tests below are not decoration. A scanner that quietly matches nothing
would leave this file passing forever while enforcing nothing, so they pin both
directions: that known-bad shapes are caught, and that the shapes the codebase
legitimately uses are not.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.suppressions import (
    format_report as format_report,
    iter_python_files as iter_python_files,
    scan_source as scan_source,
    scan_tree as scan_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_scanner_can_see_the_repository():
    """Fail loudly if the gate is pointed somewhere with no source in it.

    The packaged CI runner ``cd``s into a store copy of the tree rather than
    running from the checkout. If that copy ever stopped carrying the source,
    ``scan_tree`` would return an empty list and the conformance test below
    would pass by scanning nothing -- the exact silent no-op this whole design
    is trying to avoid.
    """
    found = list(iter_python_files(REPO_ROOT))
    assert len(found) > 100, f"only {len(found)} python files under {REPO_ROOT}; is the source tree present?"


def test_every_suppression_is_justified():
    violations = scan_tree(REPO_ROOT)
    assert not violations, (
        f"{len(violations)} suppression(s) do not say why they exist.\n"
        "Append ` -- <reason>` giving the reason the suppression is correct "
        "(not a restatement of the rule). For a whole-file pragma the reason "
        "may instead go in the comment lines directly beneath it -- and for "
        "`# pyright: rule=false` it must, because pyright rejects trailing "
        "text on its own pragma line and silently stops suppressing.\n\n" + format_report(violations)
    )


class TestScannerCatchesUnjustified:
    """The scanner must actually fire. One case per directive form."""

    def test_bare_noqa_with_codes(self):
        assert scan_source("x = 1  # noqa: E501\n", Path("s.py"))

    def test_bare_type_ignore(self):
        assert scan_source("x = 1  # type: ignore[arg-type]\n", Path("s.py"))

    def test_bare_pyright_ignore(self):
        assert scan_source("x = 1  # pyright: ignore[reportAny]\n", Path("s.py"))

    def test_unjustified_file_level_pragma(self):
        assert scan_source("# ruff: noqa: T201\nx = 1\n", Path("s.py"))

    def test_chained_directives_with_no_prose_anywhere(self):
        """A second directive is not a justification for the first."""
        assert scan_source("x = 1  # type: ignore[a]  # noqa: SLF001\n", Path("s.py"))


class TestScannerAcceptsJustified:
    """...and must not fire on the shapes the codebase actually uses."""

    def test_inline_reason(self):
        assert not scan_source("x = 1  # noqa: E501 -- url in a docstring\n", Path("s.py"))

    def test_one_reason_covers_a_chained_pair(self):
        source = "x = obj._p  # type: ignore[reportPrivateUsage] -- owner reaches into its own collaborator  # noqa: SLF001\n"
        assert not scan_source(source, Path("s.py"))

    def test_file_level_reason_on_the_line_beneath(self):
        assert not scan_source("# ruff: noqa: T201\n# output is the point here\nx = 1\n", Path("s.py"))

    def test_file_level_reason_below_a_second_pragma(self):
        source = "# ruff: noqa: F401\n# pyright: reportUnusedImport=false\n# re-export surface\nx = 1\n"
        assert not scan_source(source, Path("s.py"))


class TestScannerIgnoresProseAboutDirectives:
    """The false-positive class that broke the first draft of this scanner.

    A comment *documenting* the convention quotes a directive verbatim, and the
    quoted copy is character-identical to a real one. Ruff has the same bug --
    it read this repo's comment about a file-level pragma as a malformed
    pragma. Position is the discriminator: a real inline directive trails code
    or opens its comment; prose about one does neither.
    """

    def test_directive_quoted_mid_sentence_in_a_standalone_comment(self):
        assert not scan_source("# Write `# noqa: E501` when the line is a URL.\nx = 1\n", Path("s.py"))

    def test_directive_named_inside_a_string_is_not_a_comment(self):
        assert not scan_source('MARKER = "# type: ignore[arg-type]"\n', Path("s.py"))

    def test_pragma_described_in_a_standalone_comment(self):
        assert not scan_source("# The ruff: noqa pragma suppresses a whole file.\nx = 1\n", Path("s.py"))
