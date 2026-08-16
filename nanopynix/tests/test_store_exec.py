"""Can a program that lives in a *relocated* store actually be run?

A store opened with a root reports ordinary logical ``/nix/store/...`` paths
while the bytes live under that root. Every test store in this suite is
relocated that way, so a store path this suite produces is *not* at the
location it names -- and exec'ing it therefore fails with ``FileNotFoundError``
on a path that is perfectly valid.

That is not a hypothetical: it is how the terranix LSP scenarios failed on a
fresh CI runner while passing on any developer machine that happened to have
the same derivation in its host store already. ``nanopynix.store_exec_prefix``
(and the ``nanopynix-store-exec`` helper behind it) is the fix.

The assertions below are ordered so the test cannot pass vacuously: it first
proves the path really is absent at its logical location -- without that, a
host store that happens to contain the path would make the interesting half
of the test pass for the wrong reason -- and only then runs it through the
prefix.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest

import nanopynix
from nanopynix_testing.nix_markers import LINUX_STORE_EXEC
from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import InprocSessionFactory, RpcSessionFactory

# The interpreter of this process, and **not** `/bin/sh`.
#
# The script below is only a program to exec: what the test asks is whether a
# program that lives in a relocated store runs at all. The interpreter is
# incidental, and a host one costs the test its hermeticity.
#
# `/bin/sh` cost it exactly that under ASAN. That build sets `LD_PRELOAD` to
# the sanitizer runtime, `LD_PRELOAD` reaches every child, and the runtime
# links against the glibc of this closure, so the host shell got two glibcs:
#
#   /bin/sh: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_ABI_DT_X86_64_PLT'
#   not found (required by /nix/store/...-glibc-2.42-67/...)
#
# Both tests below then failed, on every ASAN run. `nix/sanitizer.nix` records
# the same failure against the host git, and `nanopynix/tests.nix` answers it
# the same way: take the program from this closure. `sys.executable` is that,
# with no new input to declare.
_SCRIPT = f"#!{sys.executable}\nprint('store-exec-ok')\n"


async def _check_runs_from_a_relocated_store(store: Any, tmp_path: Path) -> None:
    source = anyio.Path(tmp_path) / "payload"
    await source.mkdir(parents=True, exist_ok=True)
    script = source / "run.sh"
    await script.write_text(_SCRIPT)
    await script.chmod(0o755)

    added = str(await store.add_to_store(str(source)))
    logical = Path(added) / "run.sh"

    prefix = await nanopynix.store_exec_prefix(store)
    if not prefix:
        pytest.skip("this store is not relocated, so there is nothing for store_exec_prefix to do")

    # The half that keeps the rest honest. If the logical path existed, the
    # run below would prove nothing about the helper.
    assert not logical.exists(), (
        f"{logical} exists at its logical location, so this store is not relocated after all "
        f"and the rest of this test would pass without the helper doing anything"
    )

    result = await run_process([*prefix, str(logical)])
    assert result.returncode == 0, result.describe()
    assert "store-exec-ok" in result.stdout, result.describe()


@LINUX_STORE_EXEC
async def test_inproc_a_program_in_a_relocated_store_runs_through_the_exec_prefix(
    inproc_session: InprocSessionFactory, tmp_path: Path
) -> None:
    async with inproc_session() as session, session.store() as store:
        await _check_runs_from_a_relocated_store(store, tmp_path)


@LINUX_STORE_EXEC
async def test_rpc_a_program_in_a_relocated_store_runs_through_the_exec_prefix(
    rpc_session: RpcSessionFactory, tmp_path: Path
) -> None:
    async with rpc_session() as session, session.store() as store:
        await _check_runs_from_a_relocated_store(store, tmp_path)
