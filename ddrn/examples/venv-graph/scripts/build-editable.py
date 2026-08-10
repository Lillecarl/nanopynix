"""Build one local project as a PEP 660 editable, and install it.

`graph.nix` runs this as the builder of an editable node of the graph.

**An editable install is a redirection, and the redirection has to survive the
store.** A backend that follows PEP 660 writes a wheel that carries no code.
It carries a path instead, and that path is the directory the backend ran in.
Here that directory is a copy of the project inside the store, which is read
only and which the user does not edit. So this script rewrites the path that
the wheel recorded into the path that `graph.nix` was told to use.

`pyproject.nix` does the same in two steps, `build-editable` and
`patch-editable` (`build/hooks/editable_hook`), and it wraps the replacement in
`os.path.expandvars`. This script keeps that wrapper, because it is what makes
an environment variable a valid answer:

    editableRoot = "/home/me/Code/myapp"   ->  one fixed tree
    editableRoot = "$MYAPP_ROOT"           ->  the tree that the variable names

The second form matters for a store path. A literal root is part of what this
node builds, so a different root gives a different environment. A variable is
the same bytes for every tree, so **one environment reads whichever tree the
variable names, and nothing rebuilds.**

The environment gives:
    source         the project, copied into the store
    editableRoot   the path to redirect to, or a reference to a variable
    backend        the PEP 517 backend, as `module` or `module:attribute`
    backendPath    PYTHONPATH for the backend, from the nodes that built it
    installerPath  PYTHONPATH for `installer`, from the node that unpacked it
    out            the output path
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast


class Pep660Backend(Protocol):
    """The one hook of PEP 660 that this node calls.

    A backend is a module, and a module carries no type. The protocol says
    what the node needs from it, and the name of the method is the name that
    PEP 660 gives.
    """

    def build_editable(
        self,
        wheel_directory: str,
        config_settings: dict[str, str] | None = None,
        metadata_directory: str | None = None,
    ) -> str: ...


#: A `.pth` line that is a bare path becomes this, with the path substituted.
#: `site` runs a line that starts with `import`, and appends any other line to
#: `sys.path` verbatim. A verbatim line cannot expand a variable, so the line
#: has to become code.
PTH_LINE = "import sys; import os.path; sys.path.append(os.path.expandvars({path!r}))"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: graph.nix must set it")
    return value


def load_backend(name: str) -> Pep660Backend:
    """Import the PEP 517 backend that the project asks for."""
    module, _, attribute = name.partition(":")
    backend: object = importlib.import_module(module)
    if attribute:
        backend = getattr(backend, attribute)
    if not hasattr(backend, "build_editable"):
        raise RuntimeError(f"the backend '{name}' has no build_editable, so it does not implement PEP 660")
    return cast("Pep660Backend", backend)


def patch_pth(text: str, build_dir: str, root: str) -> tuple[str, bool]:
    """Redirect each bare path of a `.pth` file that the build directory owns."""
    lines: list[str] = []
    patched = False
    for line in text.splitlines():
        if line.startswith(build_dir):
            lines.append(PTH_LINE.format(path=root + line[len(build_dir) :]))
            patched = True
        else:
            lines.append(line)
    return "\n".join(lines) + "\n", patched


def redirect(out: Path, build_dir: str, root: str) -> int:
    """Rewrite every recorded build path under `out`. Return how many changed.

    **A `.py` file that names the build directory is not handled here.** The
    backends that write one, setuptools among them, put the path inside a
    string literal of a generated import finder. `pyproject.nix` rewrites that
    literal as a concrete syntax tree, with `libcst`, so that it changes the
    literal and nothing else. This example uses `flit_core`, which writes the
    `.pth` form only, so it raises rather than pretend to handle the other one.
    """
    changed = 0
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".pth":
            text = path.read_text(encoding="utf-8")
            new_text, patched = patch_pth(text, build_dir, root)
            if patched:
                path.write_text(new_text, encoding="utf-8")
                changed += 1
                print(f"editable: redirected {path.name}", file=sys.stderr)
        elif path.suffix == ".py" and build_dir in path.read_text(encoding="utf-8", errors="ignore"):
            raise RuntimeError(
                f"{path} names the build directory. This backend writes an import "
                "finder, and that form needs the syntax tree rewrite that "
                "pyproject.nix does with libcst."
            )
    return changed


def main() -> int:
    out = Path(env("out"))
    source = Path(env("source"))
    root = env("editableRoot")
    backend_name = env("backend")
    installer_path = env("installerPath")
    backend_path = os.environ.get("backendPath", "")  # noqa: SIM112 -- Nix names a derivation attribute in lower case

    # **The backend runs in a writable copy, and it records where it ran.**
    # The store copy is read only, and a backend is allowed to write beside the
    # source it reads.
    build_dir = Path.cwd() / "project"
    shutil.copytree(source, build_dir)
    for path in build_dir.rglob("*"):
        path.chmod(path.stat().st_mode | 0o200)

    dist = Path.cwd() / "dist"
    dist.mkdir()

    sys.path[:0] = [entry for entry in backend_path.split(":") if entry]
    os.chdir(build_dir)
    backend = load_backend(backend_name)
    wheel_name = backend.build_editable(str(dist))
    print(f"editable: {backend_name} wrote {wheel_name}", file=sys.stderr)
    os.chdir(build_dir.parent)

    # The same installer, and the same command, that an ordinary wheel node
    # runs. An editable wheel is a wheel, and PEP 376 does not treat it apart.
    subprocess.run(  # noqa: S603 -- every argument comes from the plan, and the plan is this build
        [sys.executable, "-m", "installer", "--prefix", str(out), str(dist / wheel_name)],
        check=True,
        env={**os.environ, "PYTHONPATH": installer_path},
    )

    changed = redirect(out, str(build_dir), root)
    if changed == 0:
        raise RuntimeError(
            f"nothing under {out} named the build directory {build_dir}. "
            "The install is not editable, so the environment would read the store copy."
        )

    print(f"editable: {root} is the tree that this install reads", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
