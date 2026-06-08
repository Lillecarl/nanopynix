#! /usr/bin/env python3

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from subprocess import PIPE

import structlog
from anyio import TemporaryDirectory

from pynixd.config import LocalSocketStoreSpec
from pynixd.derived_path import DerivedPath
from pynixd.goals.goal import EndGoal, Goal, GoalContext
from pynixd.goals.manager import GoalManager
from pynixd.store import LocalSocketStore
from pynixd.substitution import HttpBinaryCacheSubstituter, SubstitutionManager
from pynixd.system_features import KNOWN_FEATURES
from pynixd.types import StoreId
from pynixd.types.build import BuildResultStatus

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
)

log = structlog.get_logger()


async def async_main():
    async with TemporaryDirectory() as td:
        store = LocalSocketStore(
            LocalSocketStoreSpec(
                store_id=StoreId("local"),
                feature_matrix={"x86_64-linux": set(KNOWN_FEATURES)},
                probe=False,
                gc_enabled=False,
                store_path=Path(td),
                use_db=True,
            )
        )
        await store.start()

        proc = await asyncio.create_subprocess_exec(
            "nix",
            "eval",
            "--raw",
            "--store",
            td,
            "--file",
            "tests/nix",
            "dyn.producingDrv.drvPath",
            stdout=PIPE,
        )
        stdout, _ = await proc.communicate()
        drv_str = stdout.decode().splitlines()[0]
        log.info("drv_str", drv_str=drv_str)

        # ── Query phase ──
        query_ctx = GoalContext(
            goal_manager=GoalManager(),
            store=store,
            substitution_manager=SubstitutionManager(
                substituters=[HttpBinaryCacheSubstituter("https://cache.nixos.org")]
            ),
            end_goal=EndGoal.QUERY,
        )
        query_goal = Goal(derived_path=DerivedPath(f"{drv_str}!out!out"), ctx=query_ctx)
        await query_goal.execute()
        log.info("query_result", result=query_goal.result, status=query_goal.result.result.status if query_goal.result else None)

        # Classify all goals into will_build / will_substitute / unknown
        will_build: set[str] = set()
        will_substitute: set[str] = set()
        unknown: set[str] = set()
        seen: set[int] = set()  # dedup by id of base_store_path
        for r in query_goal.collect_results():
            if r is None:
                continue
            key = id(r.path.base_store_path())
            if key in seen:
                continue
            seen.add(key)
            path = str(r.path.base_store_path())
            status = r.result.status
            if status is BuildResultStatus.ALREADY_VALID:
                continue
            if status is BuildResultStatus.SUBSTITUTED:
                will_substitute.add(path)
            elif status is BuildResultStatus.UNKNOWN:
                unknown.add(path)
            else:
                will_build.add(path)

        log.info(
            "query_summary",
            will_build=sorted(will_build),
            will_substitute=sorted(will_substitute),
            unknown=sorted(unknown),
        )
        await query_ctx.substitution_manager.close()

        # ── Build phase ──
        build_ctx = GoalContext(
            goal_manager=GoalManager(),
            store=store,
            substitution_manager=SubstitutionManager(
                substituters=[HttpBinaryCacheSubstituter("https://cache.nixos.org")]
            ),
            end_goal=EndGoal.BUILD,
        )
        build_goal = Goal(derived_path=DerivedPath(f"{drv_str}!out!out"), ctx=build_ctx)
        await build_goal.execute()
        log.info("build_result", result=build_goal.result)
        await build_ctx.substitution_manager.close()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
