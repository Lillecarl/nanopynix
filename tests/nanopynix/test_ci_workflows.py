"""Gate: the checked-in GitHub Actions workflows match ci/workflows/*.nix.

``.github/workflows/*.yml`` is generated. ``ci/render.py`` produces it from
``ci/workflows/on_*.nix`` through nanopynix's own ``builtins.toYAML`` primop,
and until now nothing ran that renderer except a person who remembered to.
A change to a job builder that was never rendered left CI running the previous
matrix, and every job stayed green while doing so.

**This test renders again and rewrites the checked-in files when they differ**,
so a pytest run is what drives the generation. The failure it reports is
therefore already corrected in the working copy: read the diff and commit it.

It is not in ``tests/meta/``, although it reads the repository like the tests
there do. A meta test runs no Nix, and this one evaluates the flake to learn
which Nix versions the test matrix covers. ``tests/nanopynix/test_examples.py``
is the same case: a staleness gate on generated content that has to execute
the generator.

What this does not check: whether the *rendered* workflow says something
sensible. Nothing here asserts that a job exists, that a cap is the right
number, or that a step runs the command it should. `actionlint` reads the
schema, and this test reads only the one question of whether the checked-in
file is the current output of the checked-in source.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RENDERER = _REPO_ROOT / "ci" / "render.py"

# Dynamic primop registration is broken on Nix 2.31 and is not expected to be
# fixed there, and the renderer needs `builtins.toYAML` -- see
# nanopynix.primops. Every other supported version renders, so the gate still
# runs on each of them. The guard below carries no such marker: it reads the
# tree and never evaluates anything, so it must report a missing renderer on
# every version.
_requires_dynamic_primops = pytest.mark.nix_capability("dynamic_primop_registration")


def _load_renderer() -> ModuleType:
    """Import ``ci/render.py`` by path -- ``ci/`` is not a package."""
    spec = importlib.util.spec_from_file_location("ci_render", _RENDERER)
    if spec is None or spec.loader is None:
        pytest.fail(f"could not load the renderer at {_RENDERER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_renderer_is_present() -> None:
    """The "can see it" guard.

    The packaged CI runner runs from a store copy of this repository rather
    than from the checkout (``nanopynix/tests.nix``). A missing ``ci/`` there
    would make the gate below render nothing and pass, which reads exactly
    like a gate that found everything current.
    """
    assert _RENDERER.is_file(), f"the renderer is missing at {_RENDERER}"
    renderer = _load_renderer()
    sources = renderer.workflow_sources()
    assert sources, f"no ci/workflows/on_*.nix found under {renderer.WORKFLOWS_DIR}"
    for nix_file in sources:
        assert renderer.output_path(nix_file).is_file(), (
            f"{nix_file} renders to {renderer.output_path(nix_file)}, which does not exist"
        )


@_requires_dynamic_primops
async def test_checked_in_workflows_are_current() -> None:
    renderer = _load_renderer()
    rendered = await renderer.render_workflows()

    stale: dict[Path, str] = {
        path: body for path, body in rendered.items() if await anyio.Path(path).read_text() != body
    }
    if not stale:
        return

    # Write the fresh render, so that the correction comes with the report.
    written: list[Path] = []
    for path, body in sorted(stale.items()):
        try:
            await anyio.Path(path).write_text(body)
        except OSError:
            # The packaged CI runner runs from a read-only store copy, where
            # the report alone is the whole result: a runner has nothing to
            # commit. Any other reason a write fails is reported the same way,
            # by the failure below.
            continue
        written.append(path)

    names = ", ".join(sorted(path.name for path in stale))
    if written:
        pytest.fail(
            f"{names} did not match ci/workflows/*.nix, and this test has now rewritten "
            f"{len(written)} file(s). Read the diff and commit the result."
        )
    pytest.fail(
        f"{names} does not match ci/workflows/*.nix. Run `direnv exec . python ci/render.py` "
        f"in a checkout and commit the result."
    )
