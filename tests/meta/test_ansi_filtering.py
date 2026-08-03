"""No module writes its own regular expression for an ANSI escape sequence.

Nix writes the escape sequences that reach this library, so Nix owns the answer
to which bytes are an escape sequence. ``nanopynix.strip_ansi`` calls that
answer, ``nix::filterANSIEscapes``. A pattern written here is a second answer,
and every second answer this repository grew was a subset of Nix's.

There were three of them, and the scanner docstring in
``tests/support/ansi_regexes.py`` gives each one and what it missed. Prose said
"do not do this" in one place only -- ``tests/nanopynix/test_hostile_inputs.py``
asserts that no colour code reaches one exception -- and prose that covers one
call site is how the other two appeared. CLAUDE.md gives the remedy: a
convention that a machine can check belongs here.

The unit tests below are not decoration. A scanner that quietly matches nothing
would leave this file passing forever while enforcing nothing, so they pin both
directions: that each retired pattern is caught, and that the shapes the
repository legitimately uses are not.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.ansi_regexes import (
    RE_FUNCTIONS as RE_FUNCTIONS,
    format_report as format_report,
    scan_source as scan_source,
    scan_tree as scan_tree,
)
from tests.support.suppressions import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path("s.py")


def test_scanner_can_see_the_repository() -> None:
    """Fail loudly if the gate is pointed somewhere with no source in it.

    The packaged CI runner ``cd``s into a store copy of the tree rather than
    running from the checkout. A scanner that found no files would return an
    empty list, and the conformance test below would pass by scanning nothing.
    """
    found = list(iter_python_files(REPO_ROOT))
    assert len(found) > 100, f"only {len(found)} python files under {REPO_ROOT}; is the source tree present?"


def test_no_module_writes_its_own_ansi_pattern() -> None:
    violations = scan_tree(REPO_ROOT)
    assert not violations, (
        f"{len(violations)} regular expression(s) read an ANSI escape sequence.\n"
        "Call `nanopynix.strip_ansi` instead. It calls nix::filterANSIEscapes, "
        "which reads the same bytes as an escape sequence that Nix writes, "
        "including an OSC 8 hyperlink and a sequence that does not end in "
        "`m`.\n\n" + format_report(violations)
    )


class TestScannerCatchesAPattern:
    """One case for each pattern that this repository retired."""

    def test_the_strip_ansi_package_pattern(self) -> None:
        found = scan_source(r'p = re.compile(r"\x1B\[\d+(;\d+){0,2}m")' + "\n", HERE)
        assert [(v.line, v.call) for v in found] == [(1, "re.compile")]

    def test_the_fod_pattern(self) -> None:
        found = scan_source(r'_A = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")' + "\n", HERE)
        assert [(v.line, v.call) for v in found] == [(1, "re.compile")]

    def test_the_parity_test_pattern(self) -> None:
        found = scan_source(r'_A = re.compile(r"\x1b\[[0-9;]*m")' + "\n", HERE)
        assert [(v.line, v.call) for v in found] == [(1, "re.compile")]

    def test_a_plain_literal_holding_the_character(self) -> None:
        """A non-raw literal holds the character itself, not the spelling."""
        found = scan_source('re.sub("\x1b[0-9;]*m", "", text)\n', HERE)
        assert [(v.line, v.call) for v in found] == [(1, "re.sub")]

    def test_an_f_string_pattern(self) -> None:
        """The escape may sit in any constant part of an interpolated pattern."""
        found = scan_source(r'p = re.compile(rf"{prefix}\x1b\[[0-9;]*m")' + "\n", HERE)
        assert [(v.line, v.call) for v in found] == [(1, "re.compile")]

    def test_a_function_other_than_compile(self) -> None:
        """Every `re` function that takes a pattern counts, not only compile."""
        calls = {f"re.{name}" for name in RE_FUNCTIONS}
        source = "".join(f'{call}(r"\\x1b\\[[0-9;]*m", *rest)\n' for call in sorted(calls))
        assert {v.call for v in scan_source(source, HERE)} == calls


class TestScannerAllowsWhatTheRepositoryDoes:
    """The shapes that are legal, so the gate is not trained away."""

    def test_an_escape_in_a_test_fixture(self) -> None:
        """Building a sequence is how the filter gets tested."""
        assert scan_source('text = "\x1b[31;1merror\x1b[0m"\n', HERE) == []

    def test_an_escape_in_a_docstring(self) -> None:
        """`nanopynix/_ansi.py` documents each sequence that the old pattern missed."""
        assert scan_source(r'"""The pattern was \x1B\[\d+(;\d+){0,2}m."""' + "\n", HERE) == []

    def test_a_regex_that_is_not_about_escapes(self) -> None:
        assert scan_source(r'_H = re.compile(r"^\s*specified:\s+(?P<got>\S+)$")' + "\n", HERE) == []

    def test_an_escape_passed_to_a_regex_as_the_subject(self) -> None:
        """The pattern is the first argument. The text being searched is not."""
        assert scan_source('re.sub(r"error", "", "\x1b[31mred\x1b[0m")\n', HERE) == []

    def test_re_escape_of_a_sequence(self) -> None:
        """`re.escape` quotes a literal rather than reading a sequence."""
        assert scan_source('p = re.escape("\x1b[0m")\n', HERE) == []
