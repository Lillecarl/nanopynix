"""``activity_tracking`` forwards what a build monitor reads, and only then."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from nanopynix import ActivityType, ResultType
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from test_support.notes import note

if TYPE_CHECKING:
    from nanopynix.models import LogEvent
    from nanopynix_testing.nix_environment import InprocSessionFactory, RpcSessionFactory

ACT_BUILDS = ActivityType.BUILDS
ACT_BUILD = ActivityType.BUILD
RES_PROGRESS = ResultType.PROGRESS


def _expr(nonce: str) -> str:
    return f"""
derivation {{
  name = "nanopynix-activity-tracking";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "echo {nonce} > $out" ];
}}
"""


async def _build_and_record(factory: Any, *, activity_tracking: bool) -> tuple[str, list[LogEvent]]:
    events: list[LogEvent] = []

    def record(event: LogEvent | None) -> None:
        if event is not None and event.is_nix_log:
            events.append(event)

    async with factory(activity_tracking=activity_tracking) as nix:
        subscription = nix.subscribe(record)
        try:
            async with nix.store() as store:
                async with nix.eval(store) as evaluator:
                    value = await evaluator.string(_expr(uuid.uuid4().hex))
                    drv = await value.attr("drvPath").as_string()
                await store.build_paths_with_results([f"{drv}^*"])
        finally:
            subscription.unsubscribe()
    return drv, events


def _starts(events: list[LogEvent], activity_type: int) -> list[list[Any]]:
    return [e.args for e in events if e.action == "start" and e.args[2] == activity_type]


@pytest.fixture(params=["inproc", "rpc"])
def factory(
    request: pytest.FixtureRequest, inproc_session: InprocSessionFactory, rpc_session: RpcSessionFactory
) -> Any:
    return inproc_session if request.param == "inproc" else rpc_session


@LINUX_CHROOT_BUILD
async def test_a_tracked_build_starts_and_stops(factory: Any) -> None:
    drv, events = await _build_and_record(factory, activity_tracking=True)
    note(actions=[(e.action, e.args) for e in events])

    builds = [args for args in _starts(events, ACT_BUILD) if args[4] and args[4][0] == drv]
    assert builds, f"no actBuild start for {drv}"
    build_id = builds[0][0]
    stopped = {e.args[0] for e in events if e.action == "stop"}
    assert build_id in stopped, "the build's activity never stopped"

    summaries = {args[0] for args in _starts(events, ACT_BUILDS)}
    progress = [
        e.args[2] for e in events if e.action == "result" and e.args[1] == RES_PROGRESS and e.args[0] in summaries
    ]
    assert progress, "no progress on the actBuilds summary"
    # Only `done`: Nix sends `expected` as [1, 2] after one build, because its
    # worker counts the finished goal before it drops the expectation.
    assert progress[-1][0] == 1, f"the last summary update is {progress[-1]}, so the totals stay stale"


@LINUX_CHROOT_BUILD
async def test_an_untracked_build_forwards_no_stop(factory: Any) -> None:
    _, events = await _build_and_record(factory, activity_tracking=False)
    assert _starts(events, ACT_BUILD), "the control built nothing, so it proves nothing"
    assert not [e for e in events if e.action == "stop"]
    assert not [e for e in events if e.action == "result" and e.args[1] == RES_PROGRESS]
