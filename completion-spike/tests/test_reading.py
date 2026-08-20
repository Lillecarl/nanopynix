"""Reading a candidate list out of what a shell drew.

Each case here is a recording of what one shell really wrote, kept so that the
reader can be changed without starting three shells to find out what broke.
Every one of them was a defect first.

**The recordings are fed to a terminal, and the rows it shows are read.** That
is what `ShellSession` does with the bytes of a real shell, so a case here asks
the same question that a live suite asks.
"""

from __future__ import annotations

from test_support.shell_pty import candidates, completed_line, render


def test_a_backspace_erases_rather_than_disappearing() -> None:
    r"""zsh redraws by writing one character and backing over it.

    Dropping the backspace instead of applying it turned `d\bdemo store` into
    `ddemo store`, and every candidate read from that line was wrong.
    """
    assert render("d\bdemo store ")[0] == "demo store"


def test_a_private_parameter_sequence_leaves_no_text() -> None:
    r"""fish writes `\x1b[>4;0m`, which a class of `[0-9;?]` does not match."""
    assert render("\x1b[>4;0m\x1b[>4;1mdemo")[0] == "demo"


def test_an_operating_system_command_leaves_no_text() -> None:
    """An OSC string carries text, and that text would read as a candidate."""
    assert render("\x1b]0;a title\x07demo")[0] == "demo"


def test_the_columns_of_bash_are_separated() -> None:
    """bash writes names only, and puts several to a row."""
    drawn = render("demo build --\r\n--attr  --help  --version\r\n")
    assert candidates(drawn, "demo build --") == {"--attr", "--help", "--version"}


def test_the_descriptions_of_fish_are_dropped() -> None:
    """fish puts the description in brackets after two spaces."""
    drawn = render("demo build --\r\n--help  (Display this message and exit.)  --version  (Show it.)\r\n")
    assert candidates(drawn, "demo build --") == {"--help", "--version"}


def test_the_descriptions_of_zsh_are_dropped() -> None:
    """zsh separates the description with `--`, which looks like an option."""
    drawn = render("demo build --\r\n--attr      -- The attribute to build.\r\n--help      -- Display this.\r\n")
    assert candidates(drawn, "demo build --") == {"--attr", "--help"}


def test_a_redraw_over_the_command_line_joins_no_candidate() -> None:
    """The measured zsh case, and the reason the approximation needed a rule.

    zsh draws the list and then moves the cursor back to the command line,
    rather than writing a newline. A movement leaves no mark in a stream of
    bytes, so the last candidate ended up joined to the redrawn line as
    `python3Packages.richdemo build --attr`. A screen puts the redraw where the
    cursor is, and nothing joins.
    """
    drawn = render("demo build --attr \r\nhello  python3Packages.rich\x1b[1;1Hdemo build --attr hello")
    assert candidates(drawn, "demo build --attr ") == {"hello", "python3Packages.rich"}
    assert completed_line(drawn, "demo build --attr ") == "demo build --attr hello"


def test_fish_draws_the_whole_word_after_an_equals_sign() -> None:
    """The measured fish case for `demo build --attr=he`.

    fish replaces the whole word, so it offers `--attr=hello`, and its pager
    elides the beginning of the word. bash and zsh replace the value alone and
    offer `hello`. The value is what all three agree on.
    """
    drawn = render("demo build --attr=hedemo build --attr=hello\r\n…ttr=hello  …ttr=hello.x86_64-linux")
    assert candidates(drawn, "demo build --attr=he") == {"hello", "hello.x86_64-linux"}


def test_a_value_holding_no_equals_sign_is_left_alone() -> None:
    """bash and zsh answer the same line with the value already cut out."""
    drawn = render("demo build --attr=he\r\nhello  hello.x86_64-linux")
    assert candidates(drawn, "demo build --attr=he") == {"hello", "hello.x86_64-linux"}


def test_the_last_redraw_is_the_finished_line() -> None:
    """fish redraws the whole line after a carriage return.

    The approximation had to find where one redraw ended and the next began,
    because it deleted the carriage return. On a screen each redraw overwrites
    the row, so the row is the answer.
    """
    drawn = render("demo sto\rdemo store \rdemo store")
    assert completed_line(drawn, "demo sto") == "demo store"


def test_a_line_with_no_redraw_at_all_reads_as_empty() -> None:
    assert completed_line((), "demo sto") == ""


def test_the_sentinel_prompt_is_not_part_of_the_line() -> None:
    """A shell redraws its prompt with the line, and the prompt is not typed."""
    drawn = render("@@READY@@demo sto\r@@READY@@demo store")
    assert completed_line(drawn, "demo sto") == "demo store"
