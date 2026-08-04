"""Run the suite against itself, concurrently, so ThreadSanitizer sees more.

`tests/support/soak.py` holds the driver and says why this exists. This module
is only the two entry points, one for each engine, and the report a failure
prints.

The soak lives at the top level of `tests/nanopynix/` because it crosses both
engines, which is what this directory is for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.support.soak import (
    BorrowedSession,
    deal_lanes,
    discover_roster,
    lanes_from_manifest,
    run_soak,
    write_manifest,
)

if TYPE_CHECKING:
    from tests.support.nix_environment import InprocSessionFactory, RpcSessionFactory
    from tests.support.soak import SoakCandidate, SoakResult

pytestmark = pytest.mark.soak


def _report(result: SoakResult, engine: str) -> str:
    """Say which test failed, what it ran beside, and how to run it again.

    The overlap set is the point. A test that passes alone and fails here
    failed *against something*, and the driver already knows what was in
    flight, so it says so rather than leaving that to be reconstructed.
    """
    lines = [
        f"{len(result.failures)} of {len(result.events)} soaked tests failed under concurrency.",
        "Replay this exact composition with:",
        f"  pytest tests/nanopynix/test_concurrent_soak.py -k {engine} --soak-seed={result.seed} --capture=no",
        "",
    ]
    for event in result.failures:
        overlapping = result.overlapping(event)
        lines.append(f"{event.nodeid} (lane {event.lane})")
        lines.append(f"  {event.detail}")
        lines.append(f"  ran beside {len(overlapping)} test(s):")
        lines.extend(f"    {nodeid}" for nodeid in overlapping)
        lines.append("")
    lines.append(
        "Each one is a finding: either the test found a real defect, or it assumes "
        "it is alone. Fix it, or add it to DENYLIST in tests/support/soak.py with the reason."
    )
    return "\n".join(lines)


def _int_option(config: pytest.Config, name: str) -> int:
    """Read an int option. ``getoption`` is typed ``Any``, and this is the seam."""
    return int(str(config.getoption(name)))


def _per_engine(option: object, engine: str) -> Path:
    """Give each engine its own file.

    One `--soak-report` reaches both soaks, and the second would otherwise
    overwrite the first -- losing exactly the record a race needs. The same
    suffix on `--soak-manifest` lets one flag replay both.
    """
    base = Path(str(option))
    return base.with_name(f"{base.stem}-{engine}{base.suffix}")


async def _soak(
    request: pytest.FixtureRequest,
    *,
    engine: str,
    session_factory: Any,
    tmp_path: Path,
) -> None:
    config = request.config
    root = Path(config.rootpath)
    roster: list[SoakCandidate] = discover_roster(root=root, engine=engine)
    if not roster:
        pytest.skip(f"no {engine} test is eligible for the soak")

    seed = _int_option(config, "--soak-seed")
    manifest_path = config.getoption("--soak-manifest")
    if manifest_path is not None:
        import json  # noqa: PLC0415 -- only a replay reads a manifest, and only here

        recorded = json.loads(_per_engine(manifest_path, engine).read_text(encoding="utf-8"))
        lanes = lanes_from_manifest(recorded, roster)
        seed = int(recorded["seed"])
    else:
        lanes = deal_lanes(roster, seed=seed, lanes=_int_option(config, "--soak-lanes"))

    result = await run_soak(
        roster,
        session_factory=session_factory,
        tmp_path=tmp_path,
        seed=seed,
        lanes=lanes,
    )

    report_path = config.getoption("--soak-report")
    if report_path is not None:
        await write_manifest(result, _per_engine(report_path, engine))

    assert not result.failures, _report(result, engine)


async def test_soak_inproc(
    request: pytest.FixtureRequest,
    inproc_session: InprocSessionFactory,
    tmp_path: Path,
) -> None:
    """Every eligible inproc test, in overlapping lanes, on one shared Session.

    One Session, because ``_InprocProcessGuard`` allows only one per process --
    and one is what gives TSan the thread overlap it needs, since each Nix call
    goes to an executor thread.
    """
    async with inproc_session() as shared:
        borrowed = BorrowedSession(shared)

        def borrow(**_kwargs: Any) -> BorrowedSession:
            return borrowed

        await _soak(request, engine="inproc", session_factory=borrow, tmp_path=tmp_path)


async def test_soak_rpc(
    request: pytest.FixtureRequest,
    rpc_session: RpcSessionFactory,
    tmp_path: Path,
) -> None:
    """Every eligible rpc test, in overlapping lanes, each with its own Session.

    ``rpc.Session`` has no process guard, so a lane opens a real one and closes
    it. That exercises the client pool and many worker processes at once, which
    is a different surface from the inproc soak rather than a weaker one.
    """
    await _soak(
        request,
        engine="rpc",
        session_factory=rpc_session,
        tmp_path=tmp_path,
    )
