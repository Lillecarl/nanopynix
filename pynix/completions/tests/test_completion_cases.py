"""What `pynix` really offers, for each line a user can type.

**The table below is the point of this module.** `checks.completions` tested
one line, `pynix bu`, and that line is one of the five that work. Issue #213
measured the other eight, and four of them are wrong. One of the four is worse
than wrong: `pynix build --<TAB>` in fish *inserts* `print-dev-env`.

**A row that is wrong today is `xfail(strict=True)`, and it is not deleted.**
The expectation in each row is what the row must answer, so a fix that lands
turns the row green and fails the run until someone removes the mark. Issue
#105 holds that fix and #110 holds the decision about which library answers a
completion at all; the table outlives both.

The one cause behind three of the four is `clypi/_cli/main.py:754`: after a
parse that succeeds, clypi hands `list_arguments` the **root** command class,
so the root subcommands come back whatever the user has typed. The two lines
that answer with the options of `build` reach `list_arguments` down a different
path, and one of those two is right for the wrong reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from test_support.shell_pty import ShellSession

#: The shells this table runs against. Every row runs in all three: a row that
#: is right in two of them and wrong in the third is the fish fallback below,
#: and a table that ran one shell could not see it.
SHELL_NAMES = ("bash", "fish", "zsh")

#: Every subcommand of `pynix`. **The wrong answer of five rows below**, so it
#: is also the `forbidden` set of each of them.
ROOT_SUBCOMMANDS = frozenset(
    {
        "build",
        "config",
        "derivation",
        "develop",
        "eval",
        "flake",
        "log",
        "osearch",
        "path-info",
        "print-dev-env",
        "repl",
        "store",
    }
)

#: Every option of `pynix build`.
BUILD_OPTIONS = frozenset(
    {
        "--attr",
        "--copy-back",
        "--dry-run",
        "--eval-store",
        "--file",
        "--flake",
        "--namespaced",
        "--overlay-dir",
        "--print-build-logs",
        "--sandbox-path",
        "--store",
        "--substituters",
        "--trusted-public-keys",
        "--update-fod",
        "--verbosity",
    }
)

#: Every subcommand of `pynix store`.
STORE_SUBCOMMANDS = frozenset(
    {
        "add",
        "add-file",
        "add-indirect-root",
        "add-path",
        "add-perm-root",
        "add-temp-root",
        "cat",
        "compute-fs-closure",
        "diff-closures",
        "dirs",
        "ensure-path",
        "follow-links-to-store-path",
        "gc",
        "info",
        "is-valid-path",
        "list-valid-paths",
        "ls",
        "optimise",
        "path-from-hash-part",
        "query-derivation-outputs",
        "query-missing",
        "query-referrers",
        "query-substitutable-paths",
        "query-valid-derivers",
        "verify",
    }
)

#: Every subcommand of `pynix store gc`.
GC_SUBCOMMANDS = frozenset({"print-alive", "print-dead", "print-roots"})

#: What issue #105 has to change for a broken row to pass.
ROOT_INSTEAD_OF_HERE = "#105: clypi hands `list_arguments` the root command class after a parse that succeeds"

#: What issue #105 has to change for the one row that is not about the root.
NO_VALUE_COMPLETION = "#105: clypi answers with option names and has no way to complete the value of one"


@dataclass(frozen=True)
class Case:
    """One line a user can type, and what `pynix` owes them for it."""

    #: The identifier of the row, and the part of the test id a reader looks for.
    name: str
    #: What the user has typed when they press Tab.
    line: str
    #: Every candidate the shell must offer, and no other. `None` where the
    #: shell inserts a lone candidate rather than listing anything, which is
    #: what `line_after` states instead.
    candidates: frozenset[str] | None = None
    #: Candidates that must not be offered. A row states this as well as
    #: `candidates` when the wrong answer is a *known* set, because that is
    #: what a reader of a failure needs to see.
    forbidden: frozenset[str] = frozenset()
    #: The command line the shell is left showing, when the answer is
    #: unambiguous enough to be inserted.
    line_after: str | None = None
    #: The shells where this row fails today. Empty for a row that passes.
    broken_in: frozenset[str] = frozenset()
    #: Why it fails, and which issue holds the fix.
    reason: str = ""
    #: Extra rows of the same table that a reader should not lose.
    note: str = field(default="", compare=False)


CASES = (
    Case(
        name="the-root-lists-its-subcommands",
        line="pynix ",
        candidates=ROOT_SUBCOMMANDS,
    ),
    Case(
        name="a-prefix-of-one-subcommand-finishes-it",
        line="pynix bu",
        line_after="pynix build",
        note="A lone candidate is inserted rather than listed, so there is no list to read.",
    ),
    Case(
        name="a-subcommand-lists-its-own-options",
        line="pynix build ",
        candidates=BUILD_OPTIONS,
        forbidden=ROOT_SUBCOMMANDS,
        broken_in=frozenset(SHELL_NAMES),
        reason=ROOT_INSTEAD_OF_HERE,
    ),
    Case(
        name="two-dashes-list-the-options-of-that-subcommand",
        line="pynix build --",
        candidates=BUILD_OPTIONS,
        forbidden=ROOT_SUBCOMMANDS,
        broken_in=frozenset(SHELL_NAMES),
        reason=ROOT_INSTEAD_OF_HERE,
    ),
    Case(
        name="a-prefix-of-one-option-finishes-it",
        line="pynix build --at",
        line_after="pynix build --attr",
        note="Right for the wrong reason: clypi answers with every option of `build`, and the shell filters.",
    ),
    Case(
        name="after-an-option-comes-its-value",
        line="pynix build --attr ",
        forbidden=BUILD_OPTIONS,
        broken_in=frozenset(SHELL_NAMES),
        reason=NO_VALUE_COMPLETION,
        note="No `candidates` set: the right answer is the attributes of the file or the flake, and nothing here can produce one yet.",
    ),
    Case(
        name="a-subcommand-lists-its-subcommands",
        line="pynix store ",
        candidates=STORE_SUBCOMMANDS,
    ),
    Case(
        name="a-nested-subcommand-lists-its-subcommands",
        line="pynix store gc ",
        candidates=GC_SUBCOMMANDS,
    ),
    Case(
        name="two-dashes-at-the-root-list-no-subcommand",
        line="pynix --",
        forbidden=ROOT_SUBCOMMANDS,
        line_after="pynix --",
        broken_in=frozenset({"fish"}),
        reason="#213: fish matches `--` as a subsequence of `print-dev-env` and inserts it",
        note=(
            "No `candidates` set: `Pynix.options()` is empty, so the right answer is `--help` alone "
            "and clypi does not list it. bash and zsh drop the whole wrong list here, because no "
            "subcommand carries the typed `--`; fish does not, and `line_after` is what sees that."
        ),
    ),
)


def _marks(case: Case, shell: str) -> list[pytest.MarkDecorator]:
    """`xfail(strict=True)` where the row is broken, and nothing where it is not."""
    if shell not in case.broken_in:
        return []
    return [pytest.mark.xfail(strict=True, reason=f"{case.reason} ({shell})")]


#: Every row of the table, once for each shell.
ROWS = [
    pytest.param(case, shell, id=f"{case.name}-{shell}", marks=_marks(case, shell))
    for case in CASES
    for shell in SHELL_NAMES
]


@pytest.mark.parametrize(("case", "shell"), ROWS, indirect=["shell"])
def test_the_shell_offers_what_the_command_owes(case: Case, shell: ShellSession) -> None:
    answer = shell.complete(case.line)
    if case.candidates is not None:
        assert answer.candidates == set(case.candidates), answer.drawn
    forbidden = answer.candidates & set(case.forbidden)
    assert not forbidden, f"offered {sorted(forbidden)} for {case.line!r}: {answer.drawn}"
    if case.line_after is not None:
        assert answer.line == case.line_after, answer.drawn


#: The case that a candidate list alone cannot see. fish falls back to a
#: subsequence match when no candidate carries the typed prefix, and `--` is a
#: subsequence of `print-dev-env`, so the one wrong candidate that matches is
#: the one fish inserts. bash and zsh insert nothing here, so the mark names
#: fish alone -- and a fix in fish's own matching would fail this row strictly
#: rather than pass unnoticed.
@pytest.mark.parametrize(
    "shell",
    [
        pytest.param(
            name,
            marks=[pytest.mark.xfail(strict=True, reason="#213: fish matches `--` as a subsequence of `print-dev-env`")]
            if name == "fish"
            else [],
        )
        for name in SHELL_NAMES
    ],
    indirect=True,
)
def test_a_wrong_answer_is_not_put_on_the_command_line(shell: ShellSession) -> None:
    """**The defect a user meets, and not the one a test usually looks for.**

    A wrong candidate list is an annoyance. A wrong *insertion* runs a command
    the user did not ask for the moment they press Return, so the line the
    shell is left showing is asserted on its own.
    """
    typed = "pynix build --"
    answer = shell.complete(typed)
    inserted = set(answer.line.split()) - set(typed.split())
    assert not inserted & ROOT_SUBCOMMANDS, f"the shell inserted {sorted(inserted)}: {answer.drawn}"
    assert answer.line in {"", typed}, answer.drawn
