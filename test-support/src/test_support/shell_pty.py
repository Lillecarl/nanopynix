"""Drive a real fish, bash or zsh on a pty, and read back what Tab offered.

**Two suites drive a shell, so the driver is here and not in either of them.**
`completion-spike` asks whether a dynamic candidate can sit on a
cyclopts-generated script, and `pynix/completions/tests/` asks what the
installed `pynix` really offers for each line a user can type. The rule in
`tests/AGENTS.md` picks this project: the module names no Nix concept, and a
second project needs it.

**A pty is not optional.** zsh loads its completion system only in an
interactive shell, and bash binds Tab only when readline is attached to a
terminal. A pipe gets neither, so a test that used one would exercise no
completion at all.

Every fragile part of driving a shell lives in this module, and each one is
here for a reason that a person met before:

- **A sentinel prompt, and a wait for it.** The alternative is a sleep, which
  is either slow or flaky, and which becomes both on a loaded machine.
- **A fixed window size.** A shell formats a candidate list into columns for
  the width it believes it has. A narrow window puts one candidate on each
  line, which is what makes the output readable by a program.
- **No pagination.** Each shell asks "show all N possibilities?" beyond some
  count, and that question consumes the answer this module wants to read.
- **A terminal emulator, and not an approximation of one.** A shell draws a
  menu with colour and with cursor movement. `pyte` applies both and answers
  with the rows a person would see, so this module reads a screen and never
  interprets a byte itself.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
# `pexpect` ships no type information and nixpkgs carries no `types-pexpect`,
# so every call on the spawned child is an unknown member, and every local
# variable that holds what such a call returned is an unknown type. Scoped to
# this module, which is the only one that touches pexpect, and to those three
# rules. `pyte` needs none of them: it ships `py.typed`.

from __future__ import annotations

import re

# **Imported for real, and not under `TYPE_CHECKING`.** beartype resolves an
# annotation at run time when `NANOPYNIX_BEARTYPING` is set, and a name that
# only a type checker can see is an unresolvable forward reference then. The
# module is already loaded by the interpreter, so the import costs nothing.
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal

import pexpect
import pyte

#: The shells this module drives. Named here rather than imported, because the
#: driver must stay useful to a suite that has no completion library at all.
type Shell = Literal["fish", "bash", "zsh"]

#: What each shell is told to use as its prompt. Chosen to appear nowhere else
#: in the output of these tests, so a wait for it cannot end early.
PROMPT = "@@READY@@"

#: Columns and rows of the pty. **Wide, and not narrow.** A shell fits its
#: candidate list to the width it believes it has, and a narrow window makes it
#: cut a candidate short: measured at 40 columns, zsh offered
#: `--no-print-build-logs  -- Print buil`. Nothing here is longer than this, so
#: nothing is cut, and `candidates` below separates the columns instead.
#: Tall, so that nothing paginates.
WINDOW = (200, 200)

#: **Not `dumb`.** fish draws no candidate list at all on a terminal it believes
#: cannot address the cursor: measured, `demo build --attr ` returned the
#: redrawn command line and nothing else. `xterm` is the plainest type that
#: every one of these shells treats as a real terminal.
TERM = "xterm"

#: Seconds of silence that end a read. A completion is local work, so this is
#: long against the work and short against the timeout below.
SETTLE = 0.4

#: Seconds to wait for the *first* thing a shell draws after Tab, when a caller
#: gives no other number.
#:
#: **A program that starts slowly needs more than `SETTLE`, and a read that is
#: too short reports an empty answer rather than a slow one.** Measured against
#: `pynix`: fish starts the program twice for one completion, once for the
#: condition of `complete -n` and once for its candidates, and each start takes
#: about 0.15 s. With 0.4 s the driver returned no candidates and an unchanged
#: command line for `pynix build --<TAB>`, and the case table of
#: `pynix/completions/tests/` passed. With 2.0 s the same line came back as
#: `pynix build print-dev-env`, which is the defect issue #213 is about.
#:
#: The wait applies to the first chunk alone. `settle` below ends the read
#: after that, and it has to grow with this one: measured against `pynix`,
#: fish drew its first bytes at once and then went quiet for longer than 0.4 s
#: before it put `print-dev-env` on the command line, so a long `answer` with
#: the default `settle` still read a truncated answer.
ANSWER = SETTLE

#: Seconds before a shell that never answers fails the test.
TIMEOUT = 20.0

_TAB = "\t"

#: A command line whose last word is an option and its value written together,
#: as in `demo build --attr=he`. `candidates` reads it to know that a shell may
#: draw the whole word where another shell draws the value alone.
_EQUALS_FORM = re.compile(r"(?:^|\s)--[^\s=]*=\S*$")

#: What fish puts in front of a candidate whose beginning it did not draw. The
#: pager shows `…ttr=hello` for `--attr=hello`, so the beginning is gone and
#: cannot be recovered from the drawn text.
_ELISION = "…"


@dataclass(frozen=True)
class ShellSpec:
    """One shell, and what it takes to make that shell predictable."""

    name: str
    #: Started with no user configuration, so a developer's own dotfiles cannot
    #: change what a test sees.
    argv: tuple[str, ...]
    #: Sent once, before any completion. Each line is waited for.
    setup: tuple[str, ...] = field(default_factory=tuple)
    #: How this shell exports one variable. See `_export_path` for why the
    #: environment that the process started with is not sufficient.
    export: str = "export {name}={value}"


#: Keyed by `str`, and not by `Shell`. This module never calls cyclopts, so it
#: has no reason to demand the literal; it takes whatever name a caller has and
#: says plainly when that name is not one it drives.
SHELLS: dict[str, ShellSpec] = {
    # `-f` skips config.fish. fish needs no readline settings: it has no
    # "show all possibilities" question, and its pager is off for a list this
    # small.
    "fish": ShellSpec(
        name="fish",
        argv=("fish", "--no-config", "--private"),
        setup=(f"function fish_prompt; printf '{PROMPT}'; end",),
        export="set -gx {name} {value}",
    ),
    # `--norc --noprofile` for the dotfiles. The two `bind` lines are the
    # whole reason bash is readable: without `show-all-if-ambiguous` bash
    # needs two Tabs to list, and without `page-completions off` it stops to
    # ask.
    "bash": ShellSpec(
        name="bash",
        argv=("bash", "--norc", "--noprofile", "-i"),
        setup=(
            f"PS1='{PROMPT}'",
            "bind 'set show-all-if-ambiguous on'",
            "bind 'set page-completions off'",
            "bind 'set completion-query-items -1'",
            "bind 'set colored-stats off'",
            "bind 'set colored-completion-prefix off'",
        ),
    ),
    # `-f` skips zshrc. `compinit -u` skips the check on the ownership of the
    # completion directories, which a Nix store path fails.
    "zsh": ShellSpec(
        name="zsh",
        argv=("zsh", "-f", "-i"),
        setup=(
            f"PROMPT='{PROMPT}'",
            "autoload -Uz compinit && compinit -u",
            "LISTMAX=0",
            "unsetopt auto_menu",
            # The counterpart of bash's `show-all-if-ambiguous`. With
            # LIST_AMBIGUOUS set, which is the default, zsh inserts the common
            # prefix and lists nothing: `demo build --attr he` answered with
            # `hello` and no list at all, although two candidates matched.
            "unsetopt list_ambiguous",
            "zstyle ':completion:*' menu no",
            "zstyle ':completion:*' list-colors",
        ),
    ),
}


def render(text: str, size: tuple[int, int] = WINDOW) -> tuple[str, ...]:
    """*text* as the rows of a terminal that received it.

    Use it to read a recording. `ShellSession` keeps a screen of its own, so
    that the emulator sees every byte the shell writes and stays in step with
    it.
    """
    columns, rows = size
    screen = pyte.Screen(columns, rows)
    pyte.Stream(screen).feed(text)
    return _rows_of(screen)


def _rows_of(screen: pyte.Screen, first: int = 0) -> tuple[str, ...]:
    """The rows of *screen* from *first* down, with the empty tail dropped.

    A screen pads each row to its full width, so each row is stripped on the
    right.
    """
    rows = [row.rstrip() for row in screen.display[first:]]
    while rows and not rows[-1]:
        rows.pop()
    return tuple(rows)


def _visible(row: str) -> str:
    """*row* as the user reads it: no sentinel prompt, and no padding."""
    return row.replace(PROMPT, "").strip()


class ShellSession:
    """One running shell, driven to the point of a completion.

    Use it as a context manager, which is what closes the pty::

        with ShellSession("bash", env) as shell:
            shell.load(script_path)
            offered = shell.complete("prog build --attr ")
    """

    def __init__(
        self,
        shell: Shell | str,
        env: Mapping[str, str],
        cwd: str | None = None,
        settle: float = SETTLE,
        answer: float = ANSWER,
    ) -> None:
        spec = SHELLS.get(shell)
        if spec is None:
            raise ValueError(f"unknown shell {shell!r}; expected one of {', '.join(SHELLS)}")
        self.spec = spec
        self._settle = settle
        self._answer = answer
        columns, rows = WINDOW
        # **The screen sees every byte the shell writes, from the first one.**
        # A shell moves the cursor to a column it counted itself, so an
        # emulator that starts in the middle of a line puts the next redraw in
        # the wrong place. The session therefore keeps one screen for its whole
        # life, and `raw_complete` reads the rows below the point where the
        # line began. The screen holds no scrollback, so a session that draws
        # more than `WINDOW` rows loses its oldest ones. That is safe however
        # long a session lives: `raw_complete` reads from the row the cursor
        # is on, which is an index into the screen as it is now, and pyte
        # keeps the cursor on the last row once the buffer scrolls. The rows
        # it drops are the answers of earlier completions, which no caller
        # holds any more.
        self._screen = pyte.Screen(columns, rows)
        self._stream = pyte.Stream(self._screen)
        self._child = pexpect.spawn(
            spec.argv[0],
            list(spec.argv[1:]),
            env={**env, "TERM": TERM, "COLUMNS": str(columns), "LINES": str(rows)},
            cwd=cwd,
            dimensions=(rows, columns),
            timeout=TIMEOUT,
            encoding="utf-8",
            codec_errors="replace",
        )
        for line in spec.setup:
            self._run(line)
        self._export_path(env.get("PATH", ""))

    def __enter__(self) -> ShellSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._child.close(force=True)

    def load(self, path: str) -> None:
        """Source a completion script into this shell."""
        # `source` and not `.`, because fish has no `.`.
        self._run(f"source {path}")

    def _export_path(self, path: str) -> None:
        """Set PATH again, from inside the shell.

        **A shell does not always keep the PATH that its parent gave it.** The
        zsh of nixpkgs carries a compiled global `zshenv` in its own store path
        (`$out/etc/zshenv.zwc`), and that file rebuilds PATH out of HOME. A
        global `zshenv` is always sourced, and neither `-f` nor `-d` skips it,
        so there is no argument that prevents this.

        Measured: `env -i HOME=/tmp/fakehome PATH=/marker/bin zsh -f -c 'echo
        $PATH'` answered with the default PATH of the system and no `/marker/bin`
        in it. The program under completion then could not be found, and the
        dynamic completion returned "command not found" rather than candidates.
        """
        if path:
            self._run(self.spec.export.format(name="PATH", value=path))

    def _run(self, line: str) -> None:
        """Send one line, and wait for the prompt to come back."""
        self._child.send(line + "\n")
        self._wait_for_prompt()

    def _wait_for_prompt(self) -> None:
        """Wait for the sentinel prompt, and give the screen what arrived.

        `expect_exact` takes the bytes out of the pty, so a wait that does not
        feed them leaves the screen behind the real terminal by exactly that
        much.
        """
        self._child.expect_exact(PROMPT, timeout=TIMEOUT)
        # `before` and `after` are `None` before the first match, and `after`
        # is a class rather than text when the match failed. Neither can reach
        # this line, because `expect_exact` raises instead of returning, so the
        # test keeps the reader honest rather than hiding a case.
        matched = (self._child.before, self._child.after)
        self._stream.feed("".join(part for part in matched if isinstance(part, str)))

    def _read_until_quiet(self, first: float | None = None) -> None:
        """Feed the screen until the shell goes quiet for `settle`.

        *first* is how long to wait for the first chunk, for a caller that
        knows the shell has slow work to do before it draws anything.
        """
        read_one = False
        while True:
            wait = first if first is not None and not read_one else self._settle
            try:
                chunk = self._child.read_nonblocking(size=4096, timeout=wait)
            except pexpect.TIMEOUT:
                return
            except pexpect.EOF:
                raise RuntimeError(f"{self.spec.name} hung up while completing") from None
            self._stream.feed(chunk)
            read_one = True

    def raw_complete(self, line: str) -> tuple[str, ...]:
        """Type *line*, press Tab, and return the rows the shell drew.

        The line is abandoned afterwards, and the prompt waited for, so that
        one session can answer many completions.
        """
        # Whatever the previous call left behind, before this one starts. The
        # echo of the keys that abandoned the last line arrives late, and it
        # landed in the front of the next answer.
        self._read_until_quiet()
        # The row that the command line starts on. Every row above it belongs
        # to an earlier completion, or to the setup of the shell.
        start = self._screen.cursor.y
        self._child.send(line)
        # **The echo is read before Tab.** The echo of the typed line arrives
        # at once, so it would be the first chunk of the read below and the
        # long wait would never apply to what Tab produced.
        self._read_until_quiet()
        self._child.send(_TAB)
        self._read_until_quiet(first=self._answer)
        rows = _rows_of(self._screen, start)
        # Ctrl-U clears the line, Ctrl-C leaves any pager or menu, and the
        # empty line brings the prompt back so the next call starts clean.
        self._child.send("\x15\x03")
        self._child.send("\n")
        self._wait_for_prompt()
        return rows

    def complete(self, line: str) -> Completion:
        """What the shell did with *line* when Tab was pressed.

        One press answers both questions a test asks. A list of candidates is
        what an ambiguous prefix produces, and a finished command line is what
        an unambiguous one produces, and the same key produces each.
        """
        rows = self.raw_complete(line)
        return Completion(
            candidates=candidates(rows, line),
            line=completed_line(rows, line),
            drawn="\n".join(rows),
        )


@dataclass(frozen=True)
class Completion:
    """The answer of one Tab.

    `drawn` is kept so that a failing test can report what the shell really
    wrote, rather than only that a set did not match.
    """

    candidates: set[str]
    line: str
    drawn: str


def candidates(rows: Sequence[str], line: str) -> set[str]:
    """The candidates in *rows*, given that the caller typed *line*.

    **The three shells lay a candidate list out in three ways, and one rule
    reads all three.** A shell separates one column from the next by two or
    more spaces, and it separates a candidate from its description the same
    way. So each row is cut on runs of two or more spaces, and each piece is
    either a candidate or a description:

    - bash writes names only, one for each row or several to a row;
    - fish writes `--help  (Display this message and exit.)`;
    - zsh writes `--help                 -- Display this message and exit.`

    A description is told from a candidate by holding a space, by opening with
    a bracket, or by opening with zsh's `-- ` separator. A candidate never does
    any of those: it is one word, and it is a word the user could type.

    The command line is a row too, and it goes the same way: `demo build
    --attr` holds spaces. A one-word line, which is what a caller who typed a
    single word leaves, is removed by name.

    **After `--attr=he` the three shells draw the same completion in two
    shapes**, and this returns the value in both. bash and zsh draw `hello`,
    because each replaces the value alone: bash breaks a word on `=`, and the
    layer gives zsh a `compset -P '*='`. fish replaces the whole word, so it
    draws `--attr=hello`, and its pager elides the beginning of a long word to
    `…ttr=hello`. The value is what the three agree on, so a whole word is cut
    back to its value here rather than in each test.
    """
    typed = set(line.split())
    whole_word = _EQUALS_FORM.search(line) is not None
    found: set[str] = set()
    for row in rows:
        for piece in re.split(r"\s{2,}", _visible(row)):
            if not piece or piece in typed or " " in piece or piece.startswith(("(", "-- ")):
                continue
            found.add(_value_drawn(piece) if whole_word else piece)
    return found


def _value_drawn(piece: str) -> str:
    """*piece*, which may be a whole `--option=value` word, as the value.

    A piece that is already the value has no `=` in it and comes back
    unchanged, so this is safe to apply to every shell.
    """
    return piece.lstrip(_ELISION).split("=", 1)[-1]


def completed_line(rows: Sequence[str], line: str) -> str:
    """The command line that the shell was left showing.

    The command line is a row, and a shell redraws that row in place, so the
    last row that starts with the name of the program is the result. An
    unambiguous prefix is the case this answers: there is no list to read then,
    only a line that grew.
    """
    words = line.split()
    if not words:
        return ""
    drawn = [text for row in rows if (text := _visible(row)).startswith(words[0])]
    return drawn[-1] if drawn else ""
