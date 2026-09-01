"""Every project states a licence, and the split stays where `AGENTS.md` puts it.

The repository is Apache-2.0, and `nanopynix-bindings` alone is
LGPL-2.1-or-later, because that project links libnixexpr and libnixstore.
Read the `# Licensing` section of `AGENTS.md` for why the two live together
without a conflict, and for the one rule that keeps them that way: the
bindings must stay "or-later".

Three things decay here, and none of them fails a build:

- A new project arrives with no `license` at all. That is what pynixd did, and
  issue #131 found it only because a person read the file.
- `license-files` names a file that is not there. hatchling fails then, but
  only when that one distribution is built, which no gate does for every
  project.
- The "or-later" of the bindings is dropped, which turns a legal move of code
  into a violation and reports nothing.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

import pytest

from tests.support.suite_roots import REPO_ROOT, is_skipped

APACHE = "Apache-2.0"
# The one exception, and the reason it is one. Read `AGENTS.md`.
LGPL = "LGPL-2.1-or-later"
LGPL_PROJECT = "nanopynix-bindings"
EXPECTED: dict[str, str] = {
    LGPL_PROJECT: LGPL,
}


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
    """Apache-2.0 everywhere, and the LGPL in the one project that links Nix."""
    stated = project.get("license")
    assert stated, f'{name} states no licence. Add `license = "{APACHE}"`, or the LGPL one if it links Nix.'
    expected = EXPECTED.get(path.parent.name, APACHE)
    assert stated == expected, (
        f"{name} states {stated!r} and this repository expects {expected!r}. "
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


def test_the_bindings_keep_or_later() -> None:
    """The word that makes Apache-2.0 code legal to move into the bindings.

    Apache-2.0 adds a patent term that LGPL-2.1 does not permit a licensee to
    add. LGPL-3.0 does permit it, and "or-later" is what lets the bindings be
    taken as LGPL-3.0. Drop it and the incompatibility becomes real.
    """
    assert LGPL.endswith("-or-later"), "this module's own constant lost the suffix it exists to check"
    bindings = REPO_ROOT / LGPL_PROJECT / "pyproject.toml"
    stated = str(tomllib.loads(bindings.read_text())["project"]["license"])
    assert stated.endswith("-or-later"), (
        f"{LGPL_PROJECT} states {stated!r}. Without `-or-later` it cannot be taken as LGPL-3.0, "
        "and moving Apache-2.0 code from this repository into it becomes a licence violation."
    )
