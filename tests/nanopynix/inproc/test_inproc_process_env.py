"""What an in-process session leaves behind in the environment of the process.

``Session.open`` sets ``NIX_USER_CONF_FILES`` when the caller names a
``nix_conf``, because that variable is the input Nix reads in ``loadConfFile``.
The RPC worker does the same, and there it is harmless: that process is
disposable. In process it is not, so the session has to put the variable back.

These run in subprocesses on purpose. ``nix_conf`` is part of the signature the
one-session-per-process guard compares, and the rest of this suite opens its
sessions with ``nix_conf=None``. A session naming a file could therefore not
open here at all -- the guard would refuse it before ``open`` reached the line
under test.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from tests.support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

# argv: the nix.conf to name, then the value to preset, or "" for "not set".
# The child reports the variable at all three moments, because "restored" is
# only meaningful beside evidence that the session set it in the first place.
_ENVIRONMENT_SCRIPT = """
import asyncio
import json
import os
import sys
from pathlib import Path

import nanopynix.inproc as inproc

conf, preset = sys.argv[1], sys.argv[2]
if preset:
    os.environ["NIX_USER_CONF_FILES"] = preset
else:
    os.environ.pop("NIX_USER_CONF_FILES", None)
before = os.environ.get("NIX_USER_CONF_FILES")


async def main():
    session = inproc.Session(nix_conf=Path(conf), load_config=False)
    await session.open()
    during = os.environ.get("NIX_USER_CONF_FILES")
    await session.close()
    print(json.dumps({
        "before": before,
        "during": during,
        "after": os.environ.get("NIX_USER_CONF_FILES"),
    }))


asyncio.run(main())
"""


async def _report(conf: str, preset: str) -> dict[str, str | None]:
    result = await run_process([sys.executable, "-c", _ENVIRONMENT_SCRIPT, conf, preset])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout.strip().splitlines()[-1])


async def test_a_closed_session_leaves_the_variable_unset_if_it_was_unset(tmp_path: Path) -> None:
    """A session must not add the variable to a process that did not have it."""
    conf = tmp_path / "nix.conf"
    conf.write_text("cores = 1\n")

    seen = await _report(str(conf), "")

    assert seen["before"] is None
    assert seen["during"] == str(conf), "the session must set the variable while it is open"
    assert seen["after"] is None, "the session left the variable set in the process"


async def test_a_closed_session_puts_back_the_value_it_found(tmp_path: Path) -> None:
    """A value the process already had must survive the session."""
    conf = tmp_path / "nix.conf"
    conf.write_text("cores = 1\n")
    existing = str(tmp_path / "caller.conf")

    seen = await _report(str(conf), existing)

    assert seen["before"] == existing
    assert seen["during"] == str(conf), "the session's own file must win while it is open"
    assert seen["after"] == existing, "the session overwrote the caller's value permanently"
