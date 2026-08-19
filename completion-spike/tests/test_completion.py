"""What fish, bash and zsh really offer when Tab is pressed.

**These are the tests the package exists for.** Everything else here can be
right while the answer to the question is no, and the answer is only visible
from a shell.

Each test runs three times, once for each shell, and every one of them asserts
the same thing. That the three agree is itself a result: it says the layer in
`_layer` needs no per-shell exception beyond the one mechanism each shell
already has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from completion_spike.demo import CANDIDATES, DEFAULT_ENGINE

if TYPE_CHECKING:
    from pathlib import Path

    from test_support.shell_pty import ShellSession

#: Every option of `demo build`, in both spellings of the flag that has two.
#: `--no-print-build-logs` is the one that a hand-written fish renderer lost,
#: because fish keeps only the last `-l` of one `complete` line.
BUILD_OPTIONS = {
    "--attr",
    "--engine",
    "--help",
    "--no-print-build-logs",
    "--print-build-logs",
    "--version",
}


def test_a_prefix_of_one_subcommand_finishes_it(shell: ShellSession) -> None:
    """The plainest static case, three levels above the interesting one."""
    answer = shell.complete("demo sto")
    assert answer.line == "demo store", answer.drawn


def test_every_option_of_a_subcommand_is_offered(shell: ShellSession) -> None:
    """Both spellings of the negative flag, and no missing option."""
    answer = shell.complete("demo build --")
    assert answer.candidates == BUILD_OPTIONS, answer.drawn


def test_a_nested_subcommand_is_offered(shell: ShellSession) -> None:
    """`store gc print-roots` is three deep, so the path gate is real.

    The finished line is what is asserted, and not the candidate list. A lone
    candidate is inserted rather than listed: bash and zsh answered with
    `demo store gc print-roots` and an empty list, and only fish also drew it.
    """
    answer = shell.complete("demo store gc ")
    assert answer.line == "demo store gc print-roots", answer.drawn


def test_the_value_of_an_option_is_completed_by_the_program(shell: ShellSession) -> None:
    """The question this package was written to answer."""
    answer = shell.complete("demo build --attr ")
    assert answer.candidates == set(CANDIDATES[DEFAULT_ENGINE]), answer.drawn


def test_a_value_is_filtered_by_what_was_typed(shell: ShellSession) -> None:
    """The program receives the prefix, and answers with a subset of it."""
    answer = shell.complete("demo build --attr he")
    assert answer.candidates == {"hello", "hello.x86_64-linux"}, answer.drawn
    # Both candidates start with `hello`, so every shell here inserts that much
    # and offers the rest.
    assert answer.line == "demo build --attr hello", answer.drawn


def test_a_value_is_completed_against_the_rest_of_the_command_line(
    shell: ShellSession,
) -> None:
    """**The case that a one-word completion cannot answer.**

    `--engine` is typed before `--attr`, and it changes what the attributes
    are. A layer that passed only the word under the cursor would answer for
    the default engine every time, and be wrong whenever the user named the
    other one. This is `pynix --engine local build --attr <TAB>` in miniature.
    """
    answer = shell.complete("demo build --engine remote --attr ")
    assert answer.candidates == set(CANDIDATES["remote"]), answer.drawn
    assert "remote-only" in answer.candidates, answer.drawn
    # And the default engine still answers with its own list, so the test above
    # cannot pass by the layer simply ignoring the engine in both directions.
    assert answer.candidates != set(CANDIDATES[DEFAULT_ENGINE]), answer.drawn


def test_an_equals_spelling_of_the_context_option_is_read(shell: ShellSession) -> None:
    """`--engine=remote` is one word, and every shell here passes it whole."""
    answer = shell.complete("demo build --engine=remote --attr ")
    assert answer.candidates == set(CANDIDATES["remote"]), answer.drawn


def test_a_static_completion_starts_no_process(shell: ShellSession, call_log: Path) -> None:
    """**The reason for generating a static script in the first place.**

    clypi's completion protocol starts the program for every keypress, and
    `pynix` takes 1.75 s to start. A subcommand and an option must therefore be
    answered by the shell alone.
    """
    shell.complete("demo sto")
    shell.complete("demo build --")
    assert not call_log.exists(), call_log.read_text(encoding="utf-8")


def test_a_dynamic_completion_starts_exactly_one_process(
    shell: ShellSession,
    call_log: Path,
) -> None:
    """One process for one Tab, and not one for each keypress."""
    shell.complete("demo build --attr ")
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1, calls
    assert calls[0].startswith("_complete-value"), calls


def test_the_dynamic_layer_does_not_answer_another_option(shell: ShellSession) -> None:
    """The layer is bound to `--attr`, and to nothing else.

    Without the guard on the option, a wrapper answers every value on the
    command line with the attributes of the program.
    """
    answer = shell.complete("demo build --help ")
    assert not set(CANDIDATES[DEFAULT_ENGINE]) & answer.candidates, answer.drawn


def test_the_equals_form_completes_the_value(shell: ShellSession) -> None:
    """`--attr=he` is one option and one value, written without a space."""
    answer = shell.complete("demo build --attr=he")
    assert answer.candidates == {"hello", "hello.x86_64-linux"}, answer.drawn
