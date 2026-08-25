"""What `pynix` really offers, for each line a user can type.

**The table below is the point of this module.** `checks.completions` tested
one line, `pynix bu`. Issue #213 measured the other eight against clypi, and
four of them were wrong; one was worse than wrong, because no candidate carried
the typed `--`, fish fell back to a subsequence match, and `--` is a
subsequence of `print-dev-env`, so fish *inserted* it.

**Every row passes now.** Issue #214 replaced clypi with argparse and
argcomplete, which answers all nine correctly in fish, bash and zsh. The four
`xfail(strict=True)` marks came off with the library, and the table stayed --
that is what it was written for.

The expectations grew when the answers did. argcomplete offers the `--no-`
spelling of every negatable flag and the short alias of every option that has
one, and all of them are things a caller can type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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
        "copy",
        "derivation",
        "develop",
        "eval",
        "flake",
        "log",
        "path-info",
        "print-dev-env",
        "registry",
        "repl",
        "search",
        "store",
        "why-depends",
    }
)

#: Every long option of `pynix build`, both spellings of each negatable flag.
#: `argparse.BooleanOptionalAction` writes `--copy-back` and `--no-copy-back`
#: from one declaration, and a caller can type either.
BUILD_OPTIONS = frozenset(
    {
        "--attr",
        "--copy-back",
        "--dry-run",
        "--eval-store",
        "--file",
        "--flake",
        "--namespaced",
        "--no-copy-back",
        "--no-print-build-logs",
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

#: The short aliases of `pynix build`. Offered where the caller has typed
#: nothing, and filtered out by the shell as soon as they type `--`.
BUILD_SHORT = frozenset({"-A", "-f"})

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
        candidates=BUILD_OPTIONS | BUILD_SHORT,
        forbidden=ROOT_SUBCOMMANDS,
        note="clypi answered with the twelve subcommands of the root here, in all three shells.",
    ),
    Case(
        name="two-dashes-list-the-options-of-that-subcommand",
        line="pynix build --",
        candidates=BUILD_OPTIONS,
        forbidden=ROOT_SUBCOMMANDS,
        note="No short alias: the shell drops every candidate that does not carry the typed `--`.",
    ),
    Case(
        name="a-prefix-of-one-option-finishes-it",
        line="pynix build --at",
        line_after="pynix build --attr",
        note="The program answers with every option of `build`, and the shell keeps the one that carries `--at`.",
    ),
    Case(
        name="after-an-option-comes-its-value",
        line="pynix build --attr ",
        forbidden=BUILD_OPTIONS | BUILD_SHORT,
        note=(
            "No `--file` on this line, so there is nothing to evaluate and the right answer is "
            "nothing. `pynix._attr_completion.complete_attr` answers the attributes of the file "
            "when there is one, and `test_nix_equivalence.py` measures that against `nix` itself. "
            "What this row states is the other half: the parser does not answer a value with the "
            "option list, which is what clypi did."
        ),
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
        name="a-flake-reference-does-not-swallow-the-rest-of-the-line",
        line="pynix build --file {nix}#hello --at",
        line_after="pynix build --file {nix}#hello --attr",
        note=(
            "**A `#` earlier on the line used to drop everything after it.** "
            "`argcomplete.lexers.split_line` lexes with a vendored `shlex` whose `commenters` is "
            "`#`, and it never clears it, so the finder saw `pynix build --file .` and completed an "
            "empty word. The shell then offered every option of `pynix build`. No shell reads `#` "
            "that way: bash treats it as a comment only at the start of a word, and it is in no "
            "`COMP_WORDBREAKS`. `libpynix.command._let_a_hash_stay_in_the_line` corrects the lexer, "
            "at the point that already calls argcomplete. Issue #221. "
            "The unquoted spelling is the one to state here, because the quoted one always worked."
        ),
    ),
    Case(
        name="a-partial-file-path-finishes-it",
        line="pynix build --file {nix}/defau",
        line_after="pynix build --file {nix}/default.nix",
        note=(
            "**The row for issue #279, and it needs all three shells.** `--file` answered nothing "
            "before a `#` and left the file names to the shell. bash offers them, because "
            "argcomplete registers it with `complete -o default`. fish does not: the line "
            "argcomplete writes there is `complete --command pynix -f`, and `-f` turns the "
            "fall-back off, so this line offered nothing at all. `pynix._attr_completion._paths` "
            "answers it now, the way `Args::completePath` answers it for `nix`."
        ),
    ),
    Case(
        name="two-dashes-at-the-root-list-no-subcommand",
        line="pynix --",
        candidates=frozenset(),
        line_after="pynix --",
        forbidden=ROOT_SUBCOMMANDS,
        note=(
            "Nothing at all: the root takes no option of its own, and `libpynix.complete` excludes "
            "`-h` and `--help`. clypi answered with the twelve subcommands, and fish inserted "
            "`print-dev-env`. The line is unchanged, because there is nothing to insert: issue #216 "
            "is what made that assertable, because the driver read fish's no-op redraw as "
            "`pynix ------` until a terminal emulator replaced the approximation."
        ),
    ),
)


# **Two `parametrize` marks, and not one over a pair.** The `shell` fixture is
# session-scoped, so one fish, one bash and one zsh should answer the whole
# table. A single `parametrize(("case", "shell"), ROWS, indirect=["shell"])`
# does not give that: pytest keys a parametrized fixture on the *index* of its
# argument in the list, so 11 rows that all name `fish` are 11 different keys,
# and the fixture is torn down and rebuilt for every one of them.
#
# Measured, issue #275: a fish row cost 11.3 s, of which 10.06 s was one fish
# start. The 11 fish rows took 124 s, and they take 15.9 s with the two marks
# below. Stacked marks give the cross product, the index of `shell` is the
# same for every row that names one shell, and the fixture is built three
# times for the whole table.
#
# **No `xfail` here any more, and that is the result.** Four rows carried
# `xfail(strict=True)` against clypi. Issue #214 changed the library and they
# went green, so the marks came off with it. A row that breaks again belongs
# marked rather than deleted -- `git log` holds the shape.
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
@pytest.mark.parametrize("shell", SHELL_NAMES, indirect=True)
def test_the_shell_offers_what_the_command_owes(case: Case, shell: ShellSession, nix_fixture: Path) -> None:
    # A row that names no Nix file is unchanged by this: no line holds a brace
    # unless it wrote `{nix}` on purpose.
    line = case.line.format(nix=nix_fixture)
    answer = shell.complete(line)
    if case.candidates is not None:
        assert answer.candidates == set(case.candidates), answer.drawn
    forbidden = answer.candidates & set(case.forbidden)
    assert not forbidden, f"offered {sorted(forbidden)} for {line!r}: {answer.drawn}"
    if case.line_after is not None:
        assert answer.line == case.line_after.format(nix=nix_fixture), answer.drawn


#: The case that a candidate list alone cannot see. When no candidate carries
#: the typed prefix, fish falls back to a subsequence match -- and `--` is a
#: subsequence of `print-dev-env`, which is what fish inserted while clypi
#: answered this line with the subcommands of the root. bash and zsh dropped the
#: list instead and inserted nothing, so this row was wrong in one shell of the
#: three, and a candidate-list assertion could not see it in any of them.
@pytest.mark.parametrize("shell", SHELL_NAMES, indirect=True)
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
