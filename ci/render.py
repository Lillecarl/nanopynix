"""Render ci/workflows/*.nix into .github/workflows/*.yml via nanopynix's own toYAML primop.

Requires Nix >= 2.32 -- primop registration is broken on Nix 2.31 and isn't
expected to be fixed there.

Run with::

    direnv exec . python ci/render.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio
from pathlib import Path

from nanopynix import EvalSession, NixType, Session, yaml_primops

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / "ci" / "workflows"
OUTPUT_DIR = REPO_ROOT / ".github" / "workflows"

HEADER = "# GENERATED FILE -- do not edit by hand.\n# Edit ci/workflows/{name}.nix and run `direnv exec . python ci/render.py`.\n\n"


async def render_workflow(eval_: EvalSession, nix_file: Path) -> str:
    value = await eval_.string(f"builtins.toYAML (import {nix_file})")
    return await value.force_as(NixType.STRING)


async def main() -> None:
    async with (
        Session(primops=yaml_primops()) as session,
        session.store() as store,
        session.eval(store) as eval_,
    ):
        for nix_file in sorted(WORKFLOWS_DIR.glob("*.nix")):
            body = await render_workflow(eval_, nix_file)
            out_path = OUTPUT_DIR / f"{nix_file.stem}.yml"
            out_path.write_text(HEADER.format(name=nix_file.stem) + body)
            print(f"wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
