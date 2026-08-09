"""Render ci/workflows/*.nix into .github/workflows/*.yml via nanopynix's own toYAML primop.

Requires dynamic primop registration, which every supported Nix has.

Run with::

    direnv exec . python ci/render.py

You do not have to remember that command.
``tests/nanopynix/test_ci_workflows.py`` calls :func:`render_workflows` on
every pytest run, compares the result against the checked-in YAML, and writes
the fresh render when the two differ -- so an edit under ``ci/workflows/`` that
was never rendered fails the suite *and* arrives already corrected.

That test is why this module separates rendering from writing.
:func:`render_workflows` returns the text of each output file and touches no
disk, because the packaged CI runner runs from a read-only store copy of this
repository (``nanopynix/tests.nix``), where the comparison is still meaningful
and the write is not possible.
"""

# ruff: noqa: T201
# This script's entire purpose is emitting the rendered CI matrix on
# stdout for the caller to redirect; print is the interface.

from __future__ import annotations

import asyncio
from pathlib import Path

from nanopynix import yaml_primops
from nanopynix.rpc import EvalSession, Session

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / "ci" / "workflows"
OUTPUT_DIR = REPO_ROOT / ".github" / "workflows"

HEADER = "# GENERATED FILE -- do not edit by hand.\n# Edit ci/workflows/{name}.nix; a pytest run renders this file again.\n\n"


async def render_workflow(eval_: EvalSession, nix_file: Path) -> str:
    value = await eval_.string(f"builtins.toYAML (import {nix_file})")
    return await value.as_string()


def workflow_sources() -> list[Path]:
    """Every workflow entry point, in a stable order.

    Only ``on_*.nix``: ``lib.nix`` is a library of job builders that each
    entry point imports, and it renders to nothing on its own.
    """
    return sorted(WORKFLOWS_DIR.glob("on_*.nix"))


def output_path(nix_file: Path) -> Path:
    return OUTPUT_DIR / f"{nix_file.stem}.yml"


async def render_workflows() -> dict[Path, str]:
    """Render every workflow, and return the whole text of each output file.

    Writes nothing. The keys are the paths that :func:`main` writes to.
    """
    async with (
        Session(
            primops=yaml_primops(),
            experimental_features=["nix-command", "flakes"],
        ) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        rendered: dict[Path, str] = {}
        for nix_file in workflow_sources():
            body = await render_workflow(eval_, nix_file)
            rendered[output_path(nix_file)] = HEADER.format(name=nix_file.stem) + body
        return rendered


def main() -> None:
    # Sync, so that the writes below are plain `pathlib` calls. Blocking file
    # I/O inside an async function is banned repository-wide, and there is
    # nothing to overlap it with here: the render is already finished.
    rendered = asyncio.run(render_workflows())
    for out_path in sorted(OUTPUT_DIR.glob("*.yml")):
        if out_path not in rendered and out_path.read_text().startswith("# GENERATED FILE --"):
            out_path.unlink()
            print(f"removed stale {out_path}")

    for out_path, body in sorted(rendered.items()):
        out_path.write_text(body)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
