"""Does a flake reference still complete when the global registry does not?

``completeFlakeRef`` (``libcmd/installables.cc``) walks every layer that
``fetchers::getRegistries`` returns, and that function builds all four layers
before it returns any of them. So an exception from the global layer -- the
one that fetches -- discards the flag, user and system layers with it.

That is wrong: a local file is not less readable because a remote one is
unreachable. ``pynix._attr_completion._registry_references`` asks a second
time with an empty ``flake-registry``, which is Nix's own value for "no global
layer", and answers from what did work. This file pins that.

**The global layer is broken through the environment, and not through a
setting.** `NIX_CONFIG` reaches `nix` and not this program -- issue #234 -- so
a `flake-registry` written there would leave the completion on its ordinary
path and the case would pass on nothing. `NIX_CACHE_HOME` does reach it:
`getCacheDir` (`libutil/users.cc`) reads that variable first, and the download
of the global layer needs to write its cache there. Pointing it below a
regular file makes the directory impossible to create, which is the exact
failure a build sandbox produces with no network -- measured, in a
`checks.completions` run where every registry candidate went missing.

**`NIX_CONFIG_HOME` is how the user layer gets an entry.** `getConfigDir`
(`libutil/users.cc`) reads that variable first, and `getUserRegistryPath` is
that directory plus `registry.json`. A setting would not do: nanopynix
registers no fetch settings with `globalConfig`, so nothing in a `nix.conf`
reaches this program. Issue #234.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

#: The identifier this file puts in a user registry of its own.
PROBE_ENTRY = "aprobeflake"

_COMPLETE_SCRIPT = """
import json
import sys

from pynix._attr_completion import complete_flake

print(json.dumps(complete_flake(sys.argv[1], "base")))
"""

#: The registry layers Nix reads from a file, with the one that fetches off.
_LAYERS_SCRIPT = """
import json

import anyio

from nanopynix import inproc


async def main() -> None:
    async with inproc.Session(verbosity="error") as nix, nix.store("dummy://") as store:
        entries = await store.registry_entries(fetch_settings={"flake-registry": ""})
    print(json.dumps([[e.type, e.from_] for e in entries]))


anyio.run(main)
"""


@pytest.fixture
def user_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A user registry holding one entry, where both Nix and this program read it."""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "registry.json").write_text(
        json.dumps(
            {
                "version": 2,
                "flakes": [
                    {
                        "from": {"type": "indirect", "id": PROBE_ENTRY},
                        "to": {"type": "github", "owner": "an-owner", "repo": "a-repo"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NIX_CONFIG_HOME", str(directory))
    return directory


async def _complete(prefix: str) -> list[str]:
    """`complete_flake` in a subprocess of its own.

    Nix caches each registry layer in a function-local static, so the settings
    of the first call decide for the whole process. Two cases in one process
    would read one another's answer.
    """
    result = await run_process([sys.executable, "-c", _COMPLETE_SCRIPT, prefix])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout.strip().splitlines()[-1])


async def _read_layers() -> list[list[str]]:
    """The registry layers that need no network, as ``[type, from]`` pairs."""
    result = await run_process([sys.executable, "-c", _LAYERS_SCRIPT])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.usefixtures("user_registry")
async def test_the_fixture_puts_an_entry_where_nix_reads_it() -> None:
    """The control. Without it the case below could pass on an empty answer.

    **It names an empty ``flake-registry``, so it never reaches the network.**
    The control used to complete for real and let the global layer stand,
    which made it the one test here that could fail for a reason of its own.
    It did: three jobs of one CI run reported an empty answer, on a machine
    with a network, where the case below -- which cannot download at all --
    passed. A control that is less reliable than the thing it controls is
    worse than no control.
    """
    layers = await _read_layers()

    assert ["user", f"flake:{PROBE_ENTRY}"] in layers


@pytest.mark.usefixtures("user_registry")
async def test_the_user_layer_still_answers_when_the_global_layer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour this file exists for, and the one `nix` does not have.

    `nix` in the same situation offers nothing at all, because
    `getRegistries` discards every layer when one of them raises.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv("NIX_CACHE_HOME", str(blocked / "nix"))
    record = tmp_path / "completion-failure.txt"
    monkeypatch.setenv("PYNIX_COMPLETION_DEBUG", str(record))

    offered = await _complete("aprobe")

    # A completion that fails answers nothing and says nothing, so a bare
    # membership assertion names no cause. `PYNIX_COMPLETION_DEBUG` is the
    # documented way to look, and a failure in CI is where nobody can look by
    # hand.
    assert PROBE_ENTRY in offered, (
        f"offered {offered}\n"
        f"completion traceback:\n"
        f"{record.read_text(encoding='utf-8') if record.is_file() else '(none recorded)'}"
    )
