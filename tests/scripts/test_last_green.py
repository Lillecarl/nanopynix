"""``scripts/last-green.sh``, against a real repository and a real remote.

**This runs the script, so it is not a meta test.** ``tests/AGENTS.md`` gives
that line, and the directory this module sits in is the answer to it:
``scripts/`` belongs to the repository and to no project, and the tests of a
script belong beside the tests of everything else in that position.

It needs ``git`` and nothing else -- no Nix, no store and no network. The
remote is a bare repository in ``tmp_path``.

The case that earns this module is :func:`test_it_declines_to_move_backwards`.
The rest of the script is a push; that one is the reason the script exists in
the shape it does, and it is invisible until a branch quietly names an older
commit than it did an hour ago. Issue #283.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "last-green.sh"

if not _SCRIPT.is_file():
    pytest.skip(f"{_SCRIPT} is not in this copy of the repository", allow_module_level=True)
if shutil.which("git") is None:
    pytest.skip("git is not on PATH, and every case here drives it", allow_module_level=True)


def _environment(home: Path, **extra: str) -> dict[str, str]:
    """A small environment that still finds git.

    **`PATH` comes from the caller, and is not written here.** On NixOS `git`
    is a store path on the PATH of the shell, so a hand-written
    `/usr/bin:/bin` finds nothing and every case errors before it starts.

    `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` point away from the
    configuration of whoever runs the suite, so a `commit.gpgsign` or an
    `init.defaultBranch` of theirs decides nothing here.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        **extra,
    }


def _git(cwd: Path, *arguments: str) -> str:
    """One git command, with an identity so a commit works on any machine."""
    result = subprocess.run(  # noqa: S603 -- a fixed argv of git, in a directory of pytest
        ["git", *arguments],  # noqa: S607 -- git comes from PATH, as it does for the workflow
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_environment(cwd),
    )
    return result.stdout.strip()


def _run_script(work: Path, commit: str, branch: str = "last-green") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- the script under test, in a directory of pytest
        [str(_SCRIPT)],
        cwd=work,
        check=False,
        capture_output=True,
        text=True,
        env=_environment(work, LAST_GREEN_BRANCH=branch, LAST_GREEN_COMMIT=commit),
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Sequence[str]]:
    """A work tree with an ``origin`` it can push to, and three commits.

    Returns the work tree and the three shas, oldest first.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=develop", ".")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--initial-branch=develop", ".")
    _git(work, "remote", "add", "origin", str(remote))

    shas: list[str] = []
    for index in range(3):
        (work / "file").write_text(f"{index}\n", encoding="utf-8")
        _git(work, "add", "file")
        _git(work, "commit", "-m", f"commit {index}")
        shas.append(_git(work, "rev-parse", "HEAD"))
    _git(work, "push", "origin", "develop")
    return work, shas


def _branch_at(work: Path, branch: str = "last-green") -> str | None:
    """Where ``origin`` holds *branch*, or ``None`` when it holds nothing."""
    listing = _git(work, "ls-remote", "origin", f"refs/heads/{branch}")
    return listing.split()[0] if listing else None


def test_it_creates_the_branch_when_there_is_none(repository: tuple[Path, Sequence[str]]) -> None:
    work, shas = repository

    result = _run_script(work, shas[0])

    assert result.returncode == 0, result.stderr
    assert _branch_at(work) == shas[0]
    assert "does not exist yet" in result.stdout


def test_it_moves_the_branch_forward(repository: tuple[Path, Sequence[str]]) -> None:
    work, shas = repository
    _run_script(work, shas[0])

    result = _run_script(work, shas[2])

    assert result.returncode == 0, result.stderr
    assert _branch_at(work) == shas[2]


def test_it_declines_to_move_backwards(repository: tuple[Path, Sequence[str]]) -> None:
    """A run that finishes after a newer one must not rewind the branch.

    Two runs can finish out of order -- a re-run of an older commit, or a slow
    matrix on an older push. A plain force-push would point the branch at the
    older commit, and a reader would be bisecting from the wrong end with no
    sign that anything was wrong.
    """
    work, shas = repository
    _run_script(work, shas[2])

    result = _run_script(work, shas[0])

    assert result.returncode == 0, result.stderr
    assert _branch_at(work) == shas[2], "the older commit must not have moved the branch"
    assert "not ahead of" in result.stdout


def test_the_same_commit_twice_is_not_an_error(repository: tuple[Path, Sequence[str]]) -> None:
    """A re-run of the green commit says so and changes nothing."""
    work, shas = repository
    _run_script(work, shas[1])

    result = _run_script(work, shas[1])

    assert result.returncode == 0, result.stderr
    assert _branch_at(work) == shas[1]
    assert "already names" in result.stdout


def test_it_refuses_to_run_without_the_commit(repository: tuple[Path, Sequence[str]]) -> None:
    """The two values come from `env:`, and neither has a default worth having."""
    work, _ = repository

    result = _run_script(work, "")

    assert result.returncode != 0
    assert "LAST_GREEN_COMMIT" in result.stderr
