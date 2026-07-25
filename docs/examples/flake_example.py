"""Evaluate a Nix flake — lock, eval, navigate outputs.

Run with::

    python docs/examples/flake_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pygit2

from nanopynix import NixType
from nanopynix.rpc import Session

# A fixed signature keeps this example hermetic: a machine without a global
# git user.name/user.email configured (e.g. a fresh CI runner) would otherwise
# fail to commit.
_SIGNATURE = pygit2.Signature("nanopynix-example", "nanopynix-example@example.invalid")


def _init_flake(flake_dir: Path) -> None:
    """Create a minimal flake in a git repo."""
    (flake_dir / "flake.nix").write_text("""
    {
      outputs = { ... }: {
        packages.x86_64-linux.hello = "hello from flake";
        greeting = "hi";
        info = {
          name = "demo-flake";
          version = "0.1.0";
          features = [ "fast" "reliable" ];
        };
      };
    }
    """)
    repo = pygit2.init_repository(str(flake_dir))
    index = repo.index
    index.add("flake.nix")
    index.write()
    tree = index.write_tree()
    repo.create_commit("HEAD", _SIGNATURE, _SIGNATURE, "init", tree, [])


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="nanopynix-flake-example-"))
    try:
        _init_flake(tmp)

        async with (
            Session(experimental_features=["flakes"]) as session,
            session.store() as store,
            session.eval(store) as eval,
        ):
            # Lock and evaluate the flake.  write_lock_file=False keeps the
            # temp directory clean (lock is held in memory).
            outputs = await eval.eval_flake(str(tmp), write_lock_file=False)
            assert await outputs.get_type() == NixType.ATTRS

            # Navigate into attrs.
            greeting = outputs.attr("greeting")
            assert await greeting.force() == "hi"
            print("greeting:", await greeting.force())

            info = outputs.attr("info")
            assert await info.get_type() == NixType.ATTRS
            info_json = await info.to_python()
            assert isinstance(info_json, dict)
            assert info_json["name"] == "demo-flake"
            assert info_json["features"] == ["fast", "reliable"]
            print("info to_python:", info_json)

            # List all output attribute names.
            names = await outputs.attr_names()
            assert "packages" in names
            assert "greeting" in names
            assert "info" in names
            print("output attr names:", names)

    finally:
        shutil.rmtree(tmp)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
