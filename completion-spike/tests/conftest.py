"""One completion script, and one running shell, for each shell under test."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import completion_spike
import pytest
from completion_spike._layer import Shell, render_script
from completion_spike._pty import SHELLS, ShellSession
from completion_spike.demo import CALL_LOG_VARIABLE, DYNAMIC_VALUES, app

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The name each generated script gets. zsh is the reason there is a table and
#: not one suffix: nothing here autoloads it, so the name is free, and a plain
#: suffix keeps the three the same.
SCRIPT_NAME = {shell: f"demo.{shell}" for shell in SHELLS}


def _shim(directory: Path) -> Path:
    """A `demo` that runs this checkout, for a run outside the Nix build.

    Inside the build `pytestCheckHook` runs after the install and `preCheck`
    puts `$out/bin` on PATH, so the real console script is there. A developer
    running pytest from the dev shell has no such script, and a suite that
    skipped for them would be a suite nobody ran before pushing.
    """
    # The search path is written in rather than inherited. The shell session
    # gets a small environment of its own, with no PYTHONPATH in it, so a shim
    # that relied on the environment could not import this package and every
    # dynamic completion answered "command not found".
    source_root = Path(completion_spike.__file__).resolve().parent.parent
    path = directory / "demo"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(source_root)!r})\n"
        "from completion_spike.demo import main\n"
        "main()\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture(scope="session")
def demo_bin(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The directory that holds a runnable `demo`."""
    installed = shutil.which("demo")
    if installed is not None:
        return str(Path(installed).parent)
    directory = tmp_path_factory.mktemp("demo-bin")
    _shim(directory)
    return str(directory)


@pytest.fixture(params=sorted(SHELLS))
def shell_name(request: pytest.FixtureRequest) -> Shell:
    return cast("Shell", request.param)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """**An empty directory, and it stays empty.**

    fish adds the files of the working directory to a menu, because the static
    half of the script sets no `-f`. With the generated script beside the
    shell, `demo store gc ` offered `demo.fish` next to `print-roots`. So the
    script, the home directory and the call log each get their own place, and
    the shell runs where there is nothing to find.
    """
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home directory of this test, and not of the developer.

    Separate from `workdir` so that the history file a shell writes at exit
    cannot appear in a menu.
    """
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def script(shell_name: Shell, tmp_path: Path) -> Path:
    """The whole completion script, static half and dynamic half.

    Built in this process rather than by running `demo _completion`, so that
    the call log below counts only the calls a shell makes.
    """
    static = app.generate_completion(shell=shell_name)
    directory = tmp_path / "scripts"
    directory.mkdir(exist_ok=True)
    path = directory / SCRIPT_NAME[shell_name]
    path.write_text(render_script(shell_name, "demo", DYNAMIC_VALUES, static), encoding="utf-8")
    return path


@pytest.fixture
def call_log(tmp_path: Path) -> Path:
    """Where each start of `demo` records itself."""
    return tmp_path / "calls.log"


@pytest.fixture
def session_env(demo_bin: str, call_log: Path, home: Path) -> dict[str, str]:
    """The whole environment a session gets, and nothing else.

    A small environment on purpose: a shell that inherited this developer's own
    would pick up their `fish_complete_path`, their history and their aliases,
    and the suite would pass or fail for reasons outside the repository.
    """
    return {
        "PATH": os.pathsep.join([demo_bin, os.environ["PATH"]]),
        "HOME": str(home),
        CALL_LOG_VARIABLE: str(call_log),
    }


@pytest.fixture
def shell(
    shell_name: Shell,
    script: Path,
    session_env: dict[str, str],
    workdir: Path,
) -> Iterator[ShellSession]:
    """A shell that has loaded the script and can be asked to complete."""
    with ShellSession(shell_name, session_env, cwd=str(workdir)) as session:
        session.load(str(script))
        yield session
