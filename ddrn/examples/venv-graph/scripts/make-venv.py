"""Compose one virtual environment from the nodes that the planner chose.

`graph.nix` runs this as the builder of the root node of the graph, under the
interpreter that the environment is for.

**The approach comes from `pyproject.nix`.** Its `build/hooks/make-venv` merges
the store path of each package of a set into one environment, and this is the
small version of the same idea:

- `venv.EnvBuilder` makes a real virtual environment, so the result has
  `pyvenv.cfg`, `bin/python` and `bin/activate`, and an interpreter that finds
  its own `site-packages` with no `PYTHONPATH`.
- Each top-level entry of the `site-packages` of a member becomes one symlink.
- **A console script gets its shebang rewritten**, from the interpreter that
  installed it to the `bin/python` of this environment. Without that rewrite
  the script runs the interpreter of the store path, which does not see the
  environment. `pyproject.nix` calls this `write_bin`.

The environment gives:
    out        the output path
    members    the output of each node that belongs in the environment
    installed  one `name==version` for each member, one to a line
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
import sysconfig
from pathlib import Path
from venv import EnvBuilder


def site_packages_of(root: Path) -> Path:
    """The `site-packages` directory under `root`, for this interpreter."""
    scheme = "venv" if "venv" in sysconfig.get_scheme_names() else "posix_prefix"
    return Path(sysconfig.get_path("purelib", scheme=scheme, vars={"base": str(root), "platbase": str(root)}))


def link_tree(source: Path, target: Path) -> None:
    """Symlink one entry of a member into the environment.

    A collision is an error, because two packages that ship the same name give
    an environment that depends on the order of the members. Two entries with
    the same content are not a collision: a package set that ships one file
    twice is common, and the result is the same either way.
    """
    if target.exists() or target.is_symlink():
        if target.is_file() and source.is_file() and filecmp.cmp(target, source, shallow=False):
            return
        raise RuntimeError(
            f"two members provide '{target.name}' with different contents: {target.resolve()} and {source}"
        )
    target.symlink_to(source)


def is_python_shebang(line: bytes) -> bool:
    """Whether `line` runs a Python.

    The test is on the name of the interpreter, and not on its whole path.
    `installer` writes the shebang from `sys.executable`, and CPython reports
    that after it resolves the symlink, so an install through `python3` gives a
    shebang that names `python3.14`. A comparison against the path that the
    derivation used would miss it.
    """
    if not line.startswith(b"#!"):
        return False
    words = line[2:].strip().split()
    if not words:
        return False
    return Path(os.fsdecode(words[0])).name.startswith("python")


def link_script(source: Path, target: Path, venv_python: bytes) -> None:
    """Put one entry of `bin` into the environment, and correct its shebang.

    A console script that a wheel carries has the shebang of the interpreter
    that installed it. That interpreter is the one in the store, and it does
    not see this environment. The rewrite is what makes `bin/idna` find `idna`.
    """
    if source.is_symlink():
        shutil.copy(source, target, follow_symlinks=False)
        return

    with source.open("rb") as handle:
        first = handle.readline()
        if is_python_shebang(first):
            with target.open("wb") as out:
                out.write(b"#!" + venv_python + b"\n")
                shutil.copyfileobj(handle, out)
            target.chmod(source.stat().st_mode)
            return

    target.symlink_to(source)


def main() -> int:
    out = Path(os.environ["out"])  # noqa: SIM112 -- Nix names a derivation attribute in lower case
    members = [Path(member) for member in os.environ["members"].split()]  # noqa: SIM112 -- as above

    # `EnvBuilder` takes the interpreter that runs this script as the base of
    # the environment, which is the interpreter that `graph.nix` names.
    EnvBuilder(symlinks=True, with_pip=False).create(out)

    site = site_packages_of(out)
    site.mkdir(parents=True, exist_ok=True)
    binary = out / "bin"
    binary.mkdir(parents=True, exist_ok=True)

    venv_python = os.fsencode(binary / "python")

    for member in members:
        member_site = site_packages_of(member)
        if member_site.is_dir():
            for entry in sorted(member_site.iterdir()):
                link_tree(entry, site / entry.name)

        member_bin = member / "bin"
        if member_bin.is_dir():
            for entry in sorted(member_bin.iterdir()):
                target = binary / entry.name
                if target.exists() or target.is_symlink():
                    raise RuntimeError(f"two members provide the script '{entry.name}'")
                link_script(entry, target, venv_python)

    (out / "manifest").write_text(os.environ["installed"] + "\n", encoding="utf-8")  # noqa: SIM112 -- as above

    print(f"{len(members)} members, {len(list(site.iterdir()))} entries in site-packages", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
