"""An installed `pynix`, its completion scripts, and a shell that loaded one.

**These fixtures complete against what a user installs, and not against a
checkout.** Issue #105 measured the failure this exists to catch: three files
holding an ANSI-coloured help screen, each written to a path that a shell
loads. Only the built package can say whether that happened, so the gate hands
this suite the store path of the application and the suite reads `share/` out
of it.

A run in the dev shell has no such path. It renders the same three scripts with
`nix/render-completions.py`, which is the script the build itself runs, so a
developer sees the same answers without building anything. What that run cannot
see is a fault in the *installation* -- a wrong path under `share/`, or a file
`installShellCompletion` never wrote -- and `installed_prefix` says so where the
distinction matters.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from test_support.shell_pty import SHELLS, ShellSession

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The store path of the built `pynix`, when a build runs this suite.
#: `checks.completions` in nix/checks.nix sets it.
PREFIX_VARIABLE = "PYNIX_INSTALLED_PREFIX"

#: Where `installShellCompletion` puts each script, relative to that prefix.
#: A path here is a fact about the installation and not about clypi, so a
#: change to any of the three is a change a user would meet.
INSTALLED_PATHS = {
    "bash": "share/bash-completion/completions/pynix.bash",
    "fish": "share/fish/vendor_completions.d/pynix.fish",
    "zsh": "share/zsh/site-functions/_pynix",
}

#: The repository root, three directories above the one this file is in.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The renderer that the build runs, for a run that has no built package.
RENDERER = REPO_ROOT / "nix" / "render-completions.py"

#: Seconds of silence that end a read of what a shell drew.
#:
#: **The default of the driver is 0.4 s, and `pynix` is slower than that
#: between one thing it draws and the next.** Measured: fish echoed the typed
#: line at once, went quiet, and only then put `print-dev-env` on the command
#: line. A read that ended in the gap reported an unchanged line, so the row
#: that this suite exists for passed.
SETTLE = 1.5

#: Seconds to wait for the first thing a shell draws after Tab.
#:
#: **The default of the driver is 0.4 s, and it is too short for `pynix`.**
#: fish starts the program twice for one completion, once for the condition of
#: `complete -n` and once for its candidates. Measured: with 0.4 s this suite
#: read an empty answer for `pynix build --<TAB>` and every row passed; with
#: 2.0 s the same line came back as `pynix build print-dev-env`, which is the
#: defect issue #213 is about. A false pass here is the worst outcome
#: available, so the number is generous.
ANSWER = 5.0


@pytest.fixture(scope="session")
def installed_prefix() -> Path | None:
    """The store path of the built application, or None in a dev shell."""
    prefix = os.environ.get(PREFIX_VARIABLE)
    return Path(prefix) if prefix else None


@pytest.fixture(scope="session")
def pynix_bin(installed_prefix: Path | None) -> str:
    """The directory that holds a runnable `pynix`.

    The installed one when there is one. A shell completes by *starting the
    program*, so the program that answers must be the program under test.
    """
    if installed_prefix is not None:
        return str(installed_prefix / "bin")
    found = shutil.which("pynix")
    if found is None:
        pytest.fail(
            f"no `pynix` on PATH and no {PREFIX_VARIABLE} in the environment. "
            "Run this from the dev shell, or build `checks.pynix-completions`."
        )
    return str(Path(found).parent)


@pytest.fixture(scope="session")
def scripts(installed_prefix: Path | None, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One completion script for each shell, keyed by the name of the shell."""
    if installed_prefix is not None:
        found = {shell: installed_prefix / tail for shell, tail in INSTALLED_PATHS.items()}
        for shell, path in found.items():
            if not path.is_file():
                pytest.fail(f"the build installed no {shell} completion at {path}")
        return found
    directory = tmp_path_factory.mktemp("rendered")
    # The renderer that nix/mk-app.nix runs, with the arguments it passes.
    subprocess.run(  # noqa: S603 -- this interpreter, and a path from this file
        [sys.executable, str(RENDERER), "pynix", "Pynix", str(directory)],
        check=True,
        capture_output=True,
    )
    return {shell: directory / shell for shell in SHELLS}


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """**An empty directory, and it stays empty.**

    clypi's bash script ends in `complete -o default`, so bash offers the files
    of the working directory when the program answers with nothing. A file
    beside the shell would then read as a candidate of the program.
    """
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home directory of this test, and not of the developer.

    Separate from `workdir`, so the history file a shell writes at exit cannot
    appear in a menu.
    """
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def session_env(pynix_bin: str, home: Path) -> dict[str, str]:
    """The whole environment a session gets, and nothing else.

    A small environment on purpose: a shell that inherited this developer's own
    would pick up their `fish_complete_path`, their history and their aliases,
    and the suite would pass or fail for reasons outside the repository.

    **`SHELL` is not in it, and that is the point.** clypi resolves a
    completion through `Path(os.environ["SHELL"]).name` and raises when the
    variable is missing. `nix/render-completions.py` puts the right name into
    each callback instead, so every answer here proves that correction is still
    applied.
    """
    env = {
        "PATH": os.pathsep.join([pynix_bin, os.environ["PATH"]]),
        "HOME": str(home),
    }
    terminfo = os.environ.get("TERMINFO_DIRS")
    if terminfo:
        env["TERMINFO_DIRS"] = terminfo
    return env


@pytest.fixture
def shell(
    request: pytest.FixtureRequest, scripts: dict[str, Path], session_env: dict[str, str], workdir: Path
) -> Iterator[ShellSession]:
    """A shell that has loaded `pynix`'s completion script.

    Indirectly parametrized: each test names the shell it wants, because the
    table of cases marks a row broken in one shell and correct in another.
    """
    name = request.param
    with ShellSession(name, session_env, cwd=str(workdir), settle=SETTLE, answer=ANSWER) as session:
        session.load(str(scripts[name]))
        yield session
