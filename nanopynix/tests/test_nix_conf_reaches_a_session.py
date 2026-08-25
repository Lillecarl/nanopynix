"""What ``nix.conf`` says reaches a session that was given nothing.

**Nix keeps four settings registries, and ``globalConfig`` is only one of
them.** ``tests/meta`` pins that the four are disjoint, measured on every
supported version. ``initLibStore`` calls ``loadConfFile(globalConfig)``, so
the file reaches exactly what is registered there and nothing else.

libcmd is what registers the other three -- ``EvalSettings``,
``fetchers::Settings`` and ``flake::Settings`` -- and nanopynix does not link
libcmd. So each of those took its compiled default whatever the file said, and
the caller was the only source of a non-default value. Issue #234, and
``nanopynix-bindings/src/settings_util.hh`` carries the argument and the fix.

**Each case runs in a subprocess, and that is not caution.**
``initLibStore`` reads the file once for the whole process, and the pytest
process ran it long before any test did. A child is the only place a fresh
``nix.conf`` can be read at all. ``test_flake_registry.py`` needs a child for a
second reason, and states it.

**The child names the file through ``Session(nix_conf=...)``, and not through
the environment.** That parameter owns ``NIX_USER_CONF_FILES`` --
``docs/nanopynix/api/process.md`` lists it among the names a session parameter
takes over -- so this is the supported way to say "read this file", and it
needs no variable in place before the import.

It is also the shape that survives its neighbours. The first version of this
module set ``NIX_CONFIG_HOME`` in the child and passed alone, then failed in a
whole-suite run: ``nanopynix/tests/rpc/worker/test_worker_unit.py`` left
``NIX_USER_CONF_FILES=/tmp/fake-nix.conf`` in ``os.environ``, that variable
replaces the user configuration file list outright, and every child started
afterwards inherited it. The leak is fixed there, and naming the file directly
means this module does not depend on that fix holding.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import pytest

from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

#: One entry, in the format ``Registry::read`` parses. The same shape as
#: ``test_flake_registry.py``'s, which states what that format is.
_PROBE_REGISTRY: dict[str, Any] = {
    "version": 2,
    "flakes": [
        {
            "from": {"type": "indirect", "id": "aprobe"},
            "to": {"type": "github", "owner": "an-owner", "repo": "a-repo"},
        },
    ],
}

#: An eval setting and a fetch setting, one from each registry that libcmd
#: registers and this library used not to.
#:
#: ``flake-registry`` names a file rather than the default, which is a URL that
#: Nix downloads. No sandboxed gate of this repository has the network, and a
#: case that let the default stand would answer nothing there.
_NIX_CONF = "pure-eval = true\nflake-registry = {registry}\n"

_PROBE = """
import json
import os
import sys
from pathlib import Path

import anyio

import nanopynix

engine = getattr(nanopynix, sys.argv[1])


async def main() -> None:
    session = engine.Session(verbosity="error", nix_conf=Path(sys.argv[2]))
    async with session as nix, nix.store() as store:
        # No settings at all, so the file is the only possible source.
        async with nix.eval(store) as evaluator:
            from_file = await (await evaluator.string("builtins ? currentSystem")).as_bool()
        # The caller names the opposite of what the file says, and wins.
        async with nix.eval(store, eval_settings=nanopynix.NixEvalSettings(pure_eval=False)) as evaluator:
            explicit = await (await evaluator.string("builtins ? currentSystem")).as_bool()
        entries = await store.registry_entries()
    print(
        json.dumps(
            {
                "eval_from_file": from_file,
                "eval_explicit": explicit,
                "registry_from": sorted({str(entry.from_) for entry in entries}),
                # What the child saw, so a failure names its own cause rather
                # than leaving the reader to reproduce it.
                "global_pure_eval": nanopynix.list_settings().get("pure-eval"),
                "nix_environment": {k: v for k, v in os.environ.items() if k.startswith("NIX_")},
            }
        )
    )


anyio.run(main)
"""

ENGINES = ("inproc", "rpc")


@pytest.fixture
def nix_conf(tmp_path: Path) -> str:
    """One ``nix.conf`` this test wrote, and the registry it names."""
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(_PROBE_REGISTRY))
    written = tmp_path / "nix.conf"
    written.write_text(_NIX_CONF.format(registry=registry))
    return str(written)


async def _probe(engine: str, nix_conf: str) -> dict[str, Any]:
    result = await run_process([sys.executable, "-c", _PROBE, engine, nix_conf])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("engine", ENGINES)
async def test_an_eval_setting_in_the_file_reaches_an_evaluator(engine: str, nix_conf: str) -> None:
    """``pure-eval = true`` in the file, and no setting passed anywhere.

    A pure evaluator has no ``builtins.currentSystem``, so the absence of that
    attribute is the setting arriving. Measured before the fix, with the same
    value in ``NIX_CONFIG`` and a control that proved the file was read:
    ``http-connections = 7`` arrived in ``globalConfig`` and ``pure-eval``
    reached no evaluator.
    """
    answer = await _probe(engine, nix_conf)

    assert answer["eval_from_file"] is False, (
        f"pure-eval from nix.conf did not reach the evaluator. "
        f"globalConfig says pure-eval={answer['global_pure_eval']!r}, "
        f"child environment {answer['nix_environment']}"
    )


@pytest.mark.parametrize("engine", ENGINES)
async def test_an_explicit_eval_setting_beats_the_file(engine: str, nix_conf: str) -> None:
    """The file is a default, and the caller is not.

    Without this, a fix that read the file could as easily have made the file
    win, and every caller that passes a setting would silently get the other
    value. ``nix`` resolves it this way round, and issue #234 asks for it.
    """
    answer = await _probe(engine, nix_conf)

    assert answer["eval_explicit"] is True, "an explicit pure_eval=False lost to nix.conf"


@pytest.mark.parametrize("engine", ENGINES)
async def test_a_fetch_setting_in_the_file_reaches_a_session(engine: str, nix_conf: str) -> None:
    """``flake-registry`` is the setting issue #234 was filed about.

    `pynix --flake` completes a reference by walking the registry, and this
    setting is how a person turns the downloading layer off. A value in
    ``nix.conf`` did nothing here, so `docs/pynix/configuration.md` stated the
    gap.
    """
    answer = await _probe(engine, nix_conf)

    # `from_` is the reference as Nix spells it, so `flake:aprobe` rather than
    # the bare id. `pynix._attr_completion._registry_references` says why.
    assert any("aprobe" in reference for reference in answer["registry_from"]), (
        f"flake-registry from nix.conf did not reach the session: {answer['registry_from']}"
    )
