"""What each installed file is, before anything asks a shell to run it.

**Issue #105 measured the failure this catches**: three files holding an
ANSI-coloured help screen, each written to a path that a shell loads. That
happened because `installShellCompletion` knows click's protocol -- run
`env _PROG_COMPLETE=source_bash prog` and read stdout -- and the program of the
day read no such variable, so asked that way it printed its help screen and
exited 0. Nothing about that is an error, and the files are installed.

The case table in `test_completion_cases.py` would fail too, but it would fail
by way of a shell that offered nothing, and a reader would start at the wrong
end. These tests name the fault directly.
"""

from __future__ import annotations

import subprocess
import sys
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
#: that shell and not of another. Each one is argcomplete's own template.
#:
#: bash and zsh get the same file, which branches on `ZSH_VERSION`. fish gets
#: its own.
STRUCTURE = {
    "bash": ("_python_argcomplete()", "complete -o nospace -o default -o bashdefault -F _python_argcomplete pynix"),
    "fish": ("function __fish_pynix_complete", "complete --command pynix -f -a '(__fish_pynix_complete)'"),
    "zsh": ("#compdef pynix", "compdef _python_argcomplete pynix"),
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
def test_the_answer_comes_back_on_its_own_file_descriptor(shell: str, scripts: dict[str, Path]) -> None:
    """**The program's own stdout cannot corrupt a completion.**

    argcomplete runs the program with `8>&1 9>&2 1>/dev/null`, so the
    candidates come back on file descriptor 8 and everything the program writes
    to stdout or stderr is discarded. clypi and click both read stdout, where a
    stray `print` -- in a logger, in a library at import time -- silently breaks
    every completion. Issue #214 chose argcomplete partly for this.
    """
    text = scripts[shell].read_text(encoding="utf-8")
    assert "8>&1" in text, text
    assert "_ARGCOMPLETE" in text, text


@pytest.mark.parametrize("shell", SHELL_NAMES)
def test_the_script_sends_the_line_and_the_cursor(shell: str, scripts: dict[str, Path]) -> None:
    """**The shell sends the raw line, and not a list of words.**

    `COMP_LINE` and `COMP_POINT` are what let argcomplete lex the line itself,
    so a value holding a space or a quote survives. bash also sends
    `COMP_WORDBREAKS`, which is what makes `--store ssh://<TAB>` and
    `--attr=hel<TAB>` work there; issue #214 measured both failing under click.
    """
    text = scripts[shell].read_text(encoding="utf-8")
    assert "COMP_LINE" in text, text
    assert "COMP_POINT" in text, text
    if shell == "bash":
        assert "_ARGCOMPLETE_COMP_WORDBREAKS" in text, text


def test_the_renderer_writes_where_it_is_told(tmp_path: Path, renderer: Path) -> None:
    """``render-completions.py`` takes a program and a directory, in that order.

    **The guard for a stale argument.** clypi needed a display name between the
    two, and issue #214 removed it with clypi. The fixture in `conftest.py`
    kept passing it, so the renderer read that name as the directory and wrote
    the three scripts beside the working directory of the run. `source` on a
    missing file is quiet in all three shells, so every case of the suite
    reported an empty answer and the cause was two files away.

    A gate run never took that branch, because it sets
    `PYNIX_INSTALLED_PREFIX` and reads the built package instead. So no CI job
    could see it, and this states the contract that the fixture depends on.
    """
    out = tmp_path / "rendered"
    subprocess.run(  # noqa: S603 -- this interpreter, and a path from the `renderer` fixture
        [sys.executable, str(renderer), "pynix", str(out)],
        check=True,
        capture_output=True,
    )

    written = sorted(path.name for path in out.iterdir())
    assert written == sorted(SHELL_NAMES), f"the renderer wrote {written} into {out}"
    beside = sorted(path.name for path in tmp_path.iterdir())
    assert beside == ["rendered"], f"the renderer wrote outside the directory it was given: {beside}"
