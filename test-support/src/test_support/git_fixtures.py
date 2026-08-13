"""Small deterministic Git repositories used by flake tests."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pygit2

if TYPE_CHECKING:
    from pathlib import Path


_SIGNATURE = pygit2.Signature("test", "test@example.com")


def init_repo(path: Path) -> pygit2.Repository:
    return pygit2.init_repository(str(path))


def commit_files(repo: pygit2.Repository, *paths: Path, message: str = "init") -> pygit2.Oid:
    index = repo.index
    for path in paths:
        index.add(str(path.relative_to(repo.workdir)))
    index.write()
    tree = index.write_tree()
    head_target = repo.references["HEAD"].target
    if not isinstance(head_target, str):
        raise TypeError("test repository HEAD does not name a reference")
    parents: list[pygit2.Oid] = []
    with suppress(KeyError):
        parents = [repo.references[head_target].peel(pygit2.Commit).id]
    return repo.create_commit(head_target, _SIGNATURE, _SIGNATURE, message, tree, parents)


@dataclass(frozen=True)
class LinkedFlakes:
    """Three git flakes whose lock file is a graph, and not a flat map.

    ``root`` inputs both ``mid`` and ``leaf``, and points ``mid``'s own ``leaf``
    at its own with a ``follows`` edge. That gives the two shapes a map of the
    top level cannot express: ``leaf`` is reachable through ``mid`` as well as
    directly, and one edge is a path into the graph rather than a reference.

    All three are local git repositories, so a test that uses them needs no
    network.
    """

    root: Path
    mid: Path
    leaf: Path


def init_linked_flakes(path: Path) -> LinkedFlakes:
    """Build the flake graph that :class:`LinkedFlakes` describes, under *path*."""
    leaf = path / "leaf"
    mid = path / "mid"
    root = path / "root"
    for directory in (leaf, mid, root):
        directory.mkdir(parents=True)

    _write_flake(leaf, '{ outputs = { ... }: { marker = "leaf"; }; }\n')
    _write_flake(
        mid,
        f'{{\n  inputs.leaf.url = "git+file://{leaf}";\n  outputs = {{ ... }}: {{ marker = "mid"; }};\n}}\n',
    )
    _write_flake(
        root,
        "{\n"
        '  description = "the root of a linked flake graph";\n'
        f'  inputs.leaf.url = "git+file://{leaf}";\n'
        f'  inputs.mid.url = "git+file://{mid}";\n'
        '  inputs.mid.inputs.leaf.follows = "leaf";\n'
        '  outputs = { ... }: { marker = "root"; };\n'
        "}\n",
    )
    return LinkedFlakes(root=root, mid=mid, leaf=leaf)


def _write_flake(path: Path, contents: str) -> None:
    (path / "flake.nix").write_text(contents, encoding="utf-8")
    commit_files(init_repo(path), path / "flake.nix")


def init_flake_repo(path: Path, outputs_body: str = "val = 1;") -> pygit2.Repository:
    (path / "flake.nix").write_text(
        f"""
        {{
            outputs = {{ ... }}: {{
                {outputs_body}
            }};
        }}
        """,
        encoding="utf-8",
    )
    repo = init_repo(path)
    commit_files(repo, path / "flake.nix")
    return repo
