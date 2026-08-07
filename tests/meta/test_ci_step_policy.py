"""Every workflow step is one line, and no expression reaches a shell body.

``.github/workflows/*.yml`` is generated from ``ci/workflows/on_*.nix``, and a
``run:`` body written there is bash that reaches CI and nothing else. No gate
reads it: ``check-shell`` runs ``shellcheck`` over ``scripts/*.sh``, and a
string inside a Nix file is not a script to any tool. It cannot be run on a
laptop, and it cannot be run against one Nix version. Changing it costs a
render and a diff of the whole generated matrix.

So a step body belongs in ``ci/steps.nix``, where ``writeShellApplication``
shellchecks it, ``runtimeInputs`` frees it from whatever the runner image
happens to ship, and ``nix run --file . ciSteps.<name>`` runs the identical
thing here. This module keeps the two rules that make that hold.

**Rule 1 -- a ``run:`` body is one line.** One line is a command, and a command
is a thing a person can read in the Actions UI and paste into a terminal. The
moment it is two, it is a program that lives in the wrong place.

**Rule 2 -- a ``${{ ... }}`` expression never appears in a ``run:`` body.**
GitHub substitutes an expression textually before the shell sees it, so a value
that lands in a command can *become* part of that command. ``env:`` is where an
expression belongs, and it is also what lets rule 1 hold: the values that used
to force interpolation into a body were the backend, the Nix version and the
workspace path.

An expression is still correct in ``env:``, in ``if:``, in ``with:``, in a job
name and in ``strategy``. This module reads ``run`` and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"

_EXPRESSION = re.compile(r"\$\{\{")

# The rendered YAML, not `ci/workflows/*.nix`. The rule is about what GitHub
# runs, and the renderer is free to build a body however it likes as long as
# the result is one line. `tests/nanopynix/test_ci_workflows.py` is what keeps
# the rendered file current with its source; this module reads the file that
# CI actually executes.
_WORKFLOWS = sorted(_WORKFLOW_DIR.glob("*.yml"))


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _steps() -> Iterator[tuple[str, str, str, str]]:
    """Yield ``(workflow, job id, step label, run body)`` for every ``run:`` step.

    ``yaml.safe_load`` is untyped, so each level is cast once rather than
    trusted. A workflow whose shape does not match is a workflow this module
    cannot police, and it must not be one this module silently passes.
    """
    for path in _WORKFLOWS:
        document = cast("dict[str, object]", yaml.safe_load(path.read_text()))
        jobs = cast("dict[str, dict[str, object]]", document.get("jobs") or {})
        for job_id, job in jobs.items():
            steps = cast("list[dict[str, object]]", job.get("steps") or [])
            for index, step in enumerate(steps):
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                label = _text(step.get("name"), _text(step.get("uses"), f"step {index}"))
                yield path.name, job_id, label, run


def test_the_workflows_are_present() -> None:
    """The "can see it" guard.

    The packaged CI runner runs from a store copy of this repository rather
    than from the checkout (``nanopynix/tests.nix``). A missing ``.github/``
    there would make every parametrised test below collect nothing and pass,
    which reads exactly like a policy that found no violation.
    """
    assert _WORKFLOWS, f"no workflow YAML found under {_WORKFLOW_DIR}"
    assert list(_steps()), f"no step with a `run:` body found in {_WORKFLOW_DIR}"


@pytest.mark.parametrize(("workflow", "job", "label", "run"), _steps())
def test_a_run_body_is_one_line(workflow: str, job: str, label: str, run: str) -> None:
    lines = run.strip().splitlines()
    assert len(lines) == 1, (
        f"{workflow}: job '{job}', step '{label}' has a {len(lines)}-line `run:` body.\n"
        f"A workflow step runs one command. Move the body to ci/steps.nix as a "
        f"writeShellApplication and call it, so shellcheck reads it and so it runs "
        f"locally with `nix run --file . ciSteps.<name>`.\n"
        f"The body was:\n{run}"
    )


@pytest.mark.parametrize(("workflow", "job", "label", "run"), _steps())
def test_a_run_body_holds_no_workflow_expression(workflow: str, job: str, label: str, run: str) -> None:
    assert not _EXPRESSION.search(run), (
        f"{workflow}: job '{job}', step '{label}' interpolates a workflow expression "
        f"into its `run:` body.\n"
        f"GitHub substitutes the value textually before the shell parses the line, so "
        f"the value can become part of the command. Put it in `env:` for that step or "
        f"job, and read it as a shell variable.\n"
        f"The body was:\n{run}"
    )
