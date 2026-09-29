"""Every project states a licence, and the licence is Apache-2.0.

No project here links Nix: the engine is huggorm's bindings, a dependency
built elsewhere. Read the `# Licensing` section of `AGENTS.md`.

Two things decay here, and neither fails a build:

- A new project arrives with no `license` at all. That is what pynixd did, and
  issue #131 found it only because a person read the file.
- `license-files` names a file that is not there. hatchling fails then, but
  only when that one distribution is built, which no gate does for every
  project.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

import pytest

from tests.support.suite_roots import REPO_ROOT, is_skipped

APACHE = "Apache-2.0"


def _projects() -> list[tuple[str, Path, dict[str, object]]]:
    """Each `pyproject.toml` that declares a distribution, with its table.

    The one at the root declares no `[project]`: it carries the pyright and
    ruff configuration of the repository and builds nothing.
    """
    found: list[tuple[str, Path, dict[str, object]]] = []
    for path in sorted(REPO_ROOT.rglob("pyproject.toml")):
        if is_skipped(path):
            continue
        loaded = tomllib.loads(path.read_text())
        project = loaded.get("project")
        if not isinstance(project, dict):
            continue
        # `cast`, for the same reason as in the check below: `tomllib` types
        # every value of a parsed table as `Any`.
        table = cast("dict[str, object]", project)
        found.append((str(path.relative_to(REPO_ROOT)), path, table))
    return found


PROJECTS = _projects()
IDS = [name for name, _, _ in PROJECTS]


def test_the_scan_finds_every_project() -> None:
    """A scan that finds nothing passes every check below without looking."""
    assert len(PROJECTS) >= 12, f"only {len(PROJECTS)} projects found: {IDS}. The scan is wrong, not the repository."


@pytest.mark.parametrize(("name", "path", "project"), PROJECTS, ids=IDS)
def test_the_project_states_the_licence_that_applies_to_it(
    name: str,
    path: Path,
    project: dict[str, object],
) -> None:
    """Apache-2.0 everywhere."""
    del path
    stated = project.get("license")
    assert stated, f'{name} states no licence. Add `license = "{APACHE}"`.'
    assert stated == APACHE, (
        f"{name} states {stated!r} and this repository expects {APACHE!r}. "
        "Read the `# Licensing` section of AGENTS.md before you change either one."
    )


@pytest.mark.parametrize(("name", "path", "project"), PROJECTS, ids=IDS)
def test_every_licence_file_of_the_project_is_there(
    name: str,
    path: Path,
    project: dict[str, object],
) -> None:
    """`license-files` cannot name a path above the source root of a distribution."""
    entries = project.get("license-files")
    assert entries, f"{name} names no `license-files`, so its built artifact carries no licence text."
    # `cast`, because `tomllib` types every value of a parsed table as `Any`,
    # and pyright then calls each element of the list unknown.
    named = [str(entry) for entry in cast("list[object]", entries)]
    missing = [entry for entry in named if not (path.parent / entry).is_file()]
    assert not missing, (
        f"{name} names {missing}, and no such file sits beside it. "
        "Copy the licence text into the project, because a path above the source root is not in the sdist."
    )
