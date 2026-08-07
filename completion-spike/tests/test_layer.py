"""The emitted shell code, as text. No shell runs here.

`test_completion.py` is the authority on whether the layer works. These tests
state the properties that a person cannot see from a passing shell test: that
the static half was not touched, and that the coupling to cyclopts is the one
narrow coupling that `_layer` documents.
"""

from __future__ import annotations

from typing import cast

import pytest
from completion_spike._layer import (
    SHELLS,
    DynamicValue,
    Shell,
    entry_point,
    render_layer,
    render_script,
)
from completion_spike.demo import DYNAMIC_VALUES, app

VALUES = (DynamicValue(command_path=("build",), option="--attr", subcommand="_complete-value"),)


@pytest.fixture(params=sorted(SHELLS))
def shell_name(request: pytest.FixtureRequest) -> Shell:
    return cast("Shell", request.param)


def test_the_static_half_is_carried_through_unchanged(shell_name: Shell) -> None:
    """**The property the whole design rests on.**

    A generator's output is not an interface. If the layer rewrote a line, a
    later cyclopts would move that line and the completion would break in a way
    that only a shell could show.
    """
    static = app.generate_completion(shell=shell_name)
    whole = render_script(shell_name, "demo", DYNAMIC_VALUES, static)
    assert whole.startswith(static)


def test_the_layer_is_added_after_the_static_half(shell_name: Shell) -> None:
    static = app.generate_completion(shell=shell_name)
    layer = render_layer(shell_name, "demo", DYNAMIC_VALUES, static)
    assert render_script(shell_name, "demo", DYNAMIC_VALUES, static).endswith(layer)


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        # cyclopts namespaces the zsh function and does not namespace the bash
        # one. A guess that fitted one was wrong for the other, which is why
        # `entry_point` reads the name instead.
        ("bash", "_demo"),
        ("zsh", "_cyclopts_demo"),
    ],
)
def test_the_generated_function_is_read_out_of_the_script(shell: Shell, expected: str) -> None:
    assert entry_point(shell, app.generate_completion(shell=shell)) == expected


def test_a_script_with_no_completion_function_is_refused() -> None:
    """A silent miss would write a wrapper that calls nothing."""
    with pytest.raises(ValueError, match="found no completion function"):
        entry_point("bash", "# nothing here\n")


def test_fish_has_no_function_to_wrap() -> None:
    """fish is additive, so it needs no name and must not pretend to have one."""
    with pytest.raises(ValueError, match="no completion function to wrap"):
        # `shell` here names a shell, and is not the `shell=True` of a
        # subprocess call, which is what S604 looks for.
        entry_point("fish", app.generate_completion(shell="fish"))  # noqa: S604 -- not a subprocess call


def test_an_unknown_shell_is_refused() -> None:
    with pytest.raises(ValueError, match="no dynamic layer"):
        render_layer("tcsh", "demo", VALUES, "")


def test_the_fish_line_stops_the_file_fallback() -> None:
    """Without `-f`, fish adds every file of the working directory.

    Measured: the menu of `demo sto` offered `store-paths.xz` beside `store`,
    because the static half carries no `-f` either.
    """
    layer = render_layer("fish", "demo", VALUES, "")
    line = next(one for one in layer.splitlines() if one.startswith("complete"))
    assert " -r " in line
    assert " -f " in line
    assert "(demo _complete-value --line (commandline -cp))" in line


def test_the_fish_substitution_is_not_quoted() -> None:
    """**Quoting the substitution stops fish expanding it.**

    Measured: with `--line "(commandline -cp)"` the program received the
    literal text `(commandline -cp)` as its argument and offered nothing. fish
    splits a command substitution on newlines only, so the unquoted form
    already arrives as one argument. This is the opposite of what bash and zsh
    need, which is why it is stated as a test.
    """
    layer = render_layer("fish", "demo", VALUES, "")
    assert '"(commandline' not in layer


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_a_wrapper_delegates_every_other_case(shell: Shell) -> None:
    """The generated function is called, and not replaced."""
    static = app.generate_completion(shell=shell)
    generated = entry_point(shell, static)
    calls = {one.strip() for one in render_layer(shell, "demo", DYNAMIC_VALUES, static).splitlines()}
    assert calls & {generated, f'{generated} "$@"'}


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_a_wrapper_tests_the_command_path(shell: Shell) -> None:
    """`--attr` belongs to `build`, so a bare `--attr` must not trigger it."""
    static = app.generate_completion(shell=shell)
    assert '"build"' in render_layer(shell, "demo", DYNAMIC_VALUES, static)
