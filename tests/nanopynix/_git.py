from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

import pygit2

if TYPE_CHECKING:
    from pathlib import Path

_SIGNATURE = pygit2.Signature("test", "test@example.com")


def init_repo(path: Path) -> pygit2.Repository:
    return pygit2.init_repository(str(path))


def commit_files(repo: pygit2.Repository, *paths: Path, message: str = "init") -> pygit2.Oid:
    index = repo.index
    for p in paths:
        index.add(str(p.relative_to(repo.workdir)))
    index.write()
    tree = index.write_tree()

    head_target = repo.references["HEAD"].target
    assert isinstance(head_target, str)
    parents: list[pygit2.Oid] = []
    with suppress(KeyError):
        parents = [repo.references[head_target].peel(pygit2.Commit).id]

    return repo.create_commit(head_target, _SIGNATURE, _SIGNATURE, message, tree, parents)


def init_flake_repo(path: Path, outputs_body: str = "val = 1;") -> pygit2.Repository:
    (path / "flake.nix").write_text(f"""
    {{
        outputs = {{ ... }}: {{
            {outputs_body}
        }};
    }}
    """)
    repo = init_repo(path)
    commit_files(repo, path / "flake.nix")
    return repo
