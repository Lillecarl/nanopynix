"""Do both engines list the flake registry that Nix itself would consult?

``completeFlakeRef`` (``libcmd/installables.cc``) walks every registry that
``fetchers::getRegistries`` returns, and that walk is why ``nix build
nixp<TAB>`` offers ``nixpkgs``. ``Store.registry_entries`` is the same walk,
and issue #229 added it so that ``pynix --flake`` can answer before the ``#``.

**Each case runs in a subprocess, and that is not caution.** Nix keeps each
registry layer in a function-local static (``registry.cc``), so the settings
of the first call decide for the whole process. The inproc engine runs inside
the pytest process, so a second case in this process would read the first
case's answer and pass for the wrong reason. A subprocess gives each case its
own set of statics, which is the only way to observe what a fresh ``pynix``
observes.

**Every case names ``flake-registry`` explicitly, and that is not caution
either.** The default is a URL, and Nix downloads it. A test that let the
default stand would need the network, which no sandboxed gate of this
repository has.

The store is ``dummy://`` because no case reaches one. A store is a parameter
only for the global layer's *download*, and an absolute path takes the branch
that reads the file directly.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import pytest

from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

#: Two entries, in the format `Registry::read` parses. The second carries a
#: `ref`, because that is what turns `nixpkgs` into `nixpkgs/nixos-unstable`
#: in the completions of Nix, and it carries `exact` as well.
_PROBE_REGISTRY: dict[str, Any] = {
    "version": 2,
    "flakes": [
        {
            "from": {"type": "indirect", "id": "aprobe"},
            "to": {"type": "github", "owner": "an-owner", "repo": "a-repo"},
        },
        {
            "from": {"type": "indirect", "id": "aprobe", "ref": "a-branch"},
            "to": {"type": "path", "path": "/nowhere"},
            "exact": True,
        },
    ],
}

_LIST_SCRIPT = """
import json
import sys

import anyio

import nanopynix

engine_name, registry = sys.argv[1], sys.argv[2]
engine = getattr(nanopynix, engine_name)


async def main() -> None:
    async with engine.Session(verbosity="error") as nix, nix.store("dummy://") as store:
        entries = await store.registry_entries(fetch_settings={"flake-registry": registry})
    print(json.dumps([[e.type, e.from_, e.to, e.exact, dict(e.extra_attrs)] for e in entries]))


anyio.run(main)
"""

ENGINES = ("inproc", "rpc")


async def _list_entries(engine: str, registry: str) -> list[list[Any]]:
    result = await run_process([sys.executable, "-c", _LIST_SCRIPT, engine, registry])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def probe_registry(tmp_path: Path) -> str:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_PROBE_REGISTRY))
    return str(path)


@pytest.mark.parametrize("engine", ENGINES)
async def test_the_global_layer_reads_the_file_that_the_setting_names(engine: str, probe_registry: str) -> None:
    entries = await _list_entries(engine, probe_registry)

    globals_ = [entry for entry in entries if entry[0] == "global"]
    assert [entry[1] for entry in globals_] == ["flake:aprobe", "flake:aprobe/a-branch"]
    assert [entry[2] for entry in globals_] == ["github:an-owner/a-repo", "path:/nowhere"]
    assert [entry[3] for entry in globals_] == [False, True]


@pytest.mark.parametrize("engine", ENGINES)
async def test_an_empty_setting_leaves_no_global_layer(engine: str) -> None:
    """The value that drops the download, and with it the GC root.

    ``getGlobalRegistry`` returns an empty registry for an empty setting
    without touching the store. That is the value a completion passes when it
    must not reach the network.
    """
    entries = await _list_entries(engine, "")

    assert [entry for entry in entries if entry[0] == "global"] == []


async def test_both_engines_list_the_same_entries(probe_registry: str) -> None:
    """The parity claim, which is the reason this lives beside the engines.

    The local layers come from this machine, so what they hold is not fixed.
    That they are the same on both engines is.
    """
    inproc_entries = await _list_entries("inproc", probe_registry)
    rpc_entries = await _list_entries("rpc", probe_registry)

    assert inproc_entries == rpc_entries


@pytest.mark.parametrize("engine", ENGINES)
async def test_every_entry_names_a_layer_that_nix_defines(engine: str, probe_registry: str) -> None:
    """`Registry::RegistryType` has five enumerators and no other value."""
    entries = await _list_entries(engine, probe_registry)

    assert {entry[0] for entry in entries} <= {"flag", "user", "system", "global", "custom"}
