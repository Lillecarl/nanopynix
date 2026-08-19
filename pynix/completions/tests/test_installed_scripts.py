"""What each installed file is, before anything asks a shell to run it.

**Issue #105 measured the failure this catches**: three files holding an
ANSI-coloured help screen, each written to a path that a shell loads.
`installShellCompletion` knows click's protocol -- run
`env _PROG_COMPLETE=source_bash prog` and read stdout -- and clypi reads no
such variable, so a program asked that way prints its help screen and exits 0.
Nothing about that is an error, and the files are installed.

The case table in `test_completion_cases.py` would fail too, but it would fail
by way of a shell that offered nothing, and a reader would start at the wrong
end. These tests name the fault directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

#: The escape character. A help screen carries colour; a completion script
#: carries none. Read as a plain character and not as a pattern, because
#: `tests/meta/test_ansi_regexes.py` keeps the ledger of files that may hold a
#: regular expression for this, and a gate does not need one.
ESCAPE = "\x1b"

#: The line each shell's script must carry, so that the file is the script of
#: that shell and not of another. Each one is clypi's own template.
STRUCTURE = {
    "bash": ("_complete_pynix()", "complete -o default -F _complete_pynix pynix"),
    "fish": ("complete -c pynix --no-files",),
    "zsh": ("#compdef pynix", "compdef _complete_pynix pynix"),
}

SHELL_NAMES = tuple(STRUCTURE)


@pytest.fixture
def script(request: pytest.FixtureRequest, scripts: dict[str, Path]) -> str:
    """The text of one shell's completion script."""
    return scripts[request.param].read_text(encoding="utf-8")


@pytest.mark.parametrize("script", SHELL_NAMES, indirect=True)
def test_the_file_is_a_script_and_not_a_help_screen(script: str) -> None:
    assert ESCAPE not in script, "the file holds an ANSI escape, so it is a help screen"
    usage = [line for line in script.splitlines() if line.strip().lower().startswith("usage:")]
    assert not usage, f"the file holds a usage line, so it is a help screen: {usage}"


@pytest.mark.parametrize("shell", SHELL_NAMES)
def test_the_file_carries_the_structure_of_its_own_shell(shell: str, scripts: dict[str, Path]) -> None:
    text = scripts[shell].read_text(encoding="utf-8")
    for line in STRUCTURE[shell]:
        assert line in text, f"{scripts[shell]} carries no {line!r}"


@pytest.mark.parametrize("shell", SHELL_NAMES)
def test_the_callback_names_the_shell_that_is_asking(shell: str, scripts: dict[str, Path]) -> None:
    """**clypi resolves a completion through the user's login shell.**

    `get_installer` does `Path(os.environ["SHELL"]).name` and raises: a
    `KeyError` where `SHELL` is unset, and a `ValueError` where the name is one
    it does not know. Either one puts a Python traceback in the terminal
    instead of candidates. `nix/render-completions.py` puts the right name into
    each callback, which is also the more correct answer -- the shell that is
    completing is the one running the script.
    """
    text = scripts[shell].read_text(encoding="utf-8")
    assert f"env SHELL={shell} _CLYPI_CURRENT_ARGS=" in text, text
