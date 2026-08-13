"""What an in-process session leaves behind in the environment of the process.

``Session.open`` writes two things into ``os.environ``: ``NIX_USER_CONF_FILES``
when the caller names a ``nix_conf``, because that variable is the input Nix
reads in ``loadConfFile``, and every name the caller passes as ``env``. The RPC
worker writes the same names and never puts them back, and there that is
correct: the worker process is disposable. In process it is not, so the session
has to restore each one.

These run in subprocesses on purpose. ``nix_conf`` and ``env`` are both part of
the signature the one-session-per-process guard compares, and the rest of this
suite opens its sessions with neither. A session naming either could therefore
not open here at all -- the guard would refuse it before ``open`` reached the
line under test.

``nanopynix/tests/test_session_env.py`` holds the refusal rules, which the two
engines share, and the rpc half of the passthrough.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

#: A name no Nix and no test fixture reads, so its only source is this module.
PROBE = "NANOPYNIX_TEST_INPROC_ENV_PROBE"

# argv: one JSON object -- the nix.conf to name or "", the names to preset with
# their values, the `env` to pass, and the names to report. The child reports
# every watched name at all three moments, because "restored" is only
# meaningful beside evidence that the session set the name in the first place.
_ENVIRONMENT_SCRIPT = """
import asyncio
import json
import os
import sys
from pathlib import Path

import nanopynix.inproc as inproc

spec = json.loads(sys.argv[1])
for name, value in spec["preset"].items():
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value

watched = spec["watched"]


def snapshot():
    return {name: os.environ.get(name) for name in watched}


before = snapshot()


async def main():
    conf = spec["nix_conf"]
    session = inproc.Session(
        nix_conf=Path(conf) if conf else None,
        load_config=False,
        env=spec["env"],
    )
    await session.open()
    during = snapshot()
    await session.close()
    print(json.dumps({"before": before, "during": during, "after": snapshot()}))


asyncio.run(main())
"""


async def _report(
    *,
    nix_conf: str = "",
    preset: dict[str, str | None] | None = None,
    env: dict[str, str] | None = None,
    watched: list[str],
) -> dict[str, dict[str, str | None]]:
    spec = {
        "nix_conf": nix_conf,
        "preset": preset or {},
        "env": env or {},
        "watched": watched,
    }
    result = await run_process([sys.executable, "-c", _ENVIRONMENT_SCRIPT, json.dumps(spec)])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout.strip().splitlines()[-1])


async def test_a_closed_session_leaves_the_variable_unset_if_it_was_unset(tmp_path: Path) -> None:
    """A session must not add the variable to a process that did not have it."""
    conf = tmp_path / "nix.conf"
    conf.write_text("cores = 1\n")

    seen = await _report(
        nix_conf=str(conf),
        preset={"NIX_USER_CONF_FILES": None},
        watched=["NIX_USER_CONF_FILES"],
    )

    assert seen["before"]["NIX_USER_CONF_FILES"] is None
    assert seen["during"]["NIX_USER_CONF_FILES"] == str(conf), "the session must set the variable while it is open"
    assert seen["after"]["NIX_USER_CONF_FILES"] is None, "the session left the variable set in the process"


async def test_a_closed_session_puts_back_the_value_it_found(tmp_path: Path) -> None:
    """A value the process already had must survive the session."""
    conf = tmp_path / "nix.conf"
    conf.write_text("cores = 1\n")
    existing = str(tmp_path / "caller.conf")

    seen = await _report(
        nix_conf=str(conf),
        preset={"NIX_USER_CONF_FILES": existing},
        watched=["NIX_USER_CONF_FILES"],
    )

    assert seen["before"]["NIX_USER_CONF_FILES"] == existing
    assert seen["during"]["NIX_USER_CONF_FILES"] == str(conf), "the session's own file must win while it is open"
    assert seen["after"]["NIX_USER_CONF_FILES"] == existing, "the session overwrote the caller's value permanently"


async def test_a_session_sets_every_name_it_was_given_and_puts_each_one_back() -> None:
    """``env`` obeys the same rule as ``nix_conf``, for a name of either kind.

    One name the process already had and one it did not, in a single session,
    because the two are restored by different branches -- ``pop`` for a name
    the session added, an assignment for a name it replaced.
    """
    replaced = f"{PROBE}_REPLACED"
    added = f"{PROBE}_ADDED"

    seen = await _report(
        preset={replaced: "the value of the caller", added: None},
        env={replaced: "the value of the session", added: "added by the session"},
        watched=[replaced, added],
    )

    assert seen["before"] == {replaced: "the value of the caller", added: None}
    assert seen["during"] == {replaced: "the value of the session", added: "added by the session"}
    assert seen["after"] == {replaced: "the value of the caller", added: None}
