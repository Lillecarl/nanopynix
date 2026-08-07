"""Reading a candidate list out of what a shell drew.

Each case here is a recording of what one shell really wrote, kept so that the
reader can be changed without starting three shells to find out what broke.
Every one of them was a defect first.
"""

from __future__ import annotations

from completion_spike._pty import candidates, completed_line, strip_escapes


def test_a_backspace_erases_rather_than_disappearing() -> None:
    r"""zsh redraws by writing one character and backing over it.

    Dropping the backspace instead of applying it turned `d\bdemo store` into
    `ddemo store`, and every candidate read from that line was wrong.
    """
    assert strip_escapes("d\bdemo store ") == "demo store "


def test_a_private_parameter_sequence_is_removed() -> None:
    r"""fish writes `\x1b[>4;0m`, which a class of `[0-9;?]` does not match."""
    assert strip_escapes("\x1b[>4;0m\x1b[>4;1mdemo") == "demo"


def test_an_operating_system_command_is_removed_whole() -> None:
    """An OSC string carries text, and that text would read as a candidate."""
    assert strip_escapes("\x1b]0;a title\x07demo") == "demo"


def test_the_columns_of_bash_are_separated() -> None:
    """bash writes names only, and puts several to a line."""
    drawn = "demo build --\n--attr  --help  --version\n"
    assert candidates(drawn, "demo build --") == {"--attr", "--help", "--version"}


def test_the_descriptions_of_fish_are_dropped() -> None:
    """fish puts the description in brackets after two spaces."""
    drawn = "demo build --\n--help  (Display this message and exit.)  --version  (Show it.)\n"
    assert candidates(drawn, "demo build --") == {"--help", "--version"}


def test_the_descriptions_of_zsh_are_dropped() -> None:
    """zsh separates the description with `--`, which looks like an option."""
    drawn = "demo build --\n--attr      -- The attribute to build.\n--help      -- Display this.\n"
    assert candidates(drawn, "demo build --") == {"--attr", "--help"}


def test_a_candidate_joined_to_the_redraw_is_still_read() -> None:
    """The measured zsh case, and the reason `_split_at_redraw` exists.

    A shell moves the cursor back to the command line rather than writing a
    newline, and that movement goes with the other escape sequences.
    """
    drawn = "demo build --attr \nhello\npython3Packages.richdemo build --attr"
    assert candidates(drawn, "demo build --attr ") == {"hello", "python3Packages.rich"}


def test_the_last_redraw_is_the_finished_line() -> None:
    """fish separates one redraw from the next with a space."""
    drawn = "demo stodemo store demo store"
    assert completed_line(drawn, "demo sto") == "demo store"


def test_a_line_with_no_redraw_at_all_reads_as_empty() -> None:
    assert completed_line("", "demo sto") == ""
