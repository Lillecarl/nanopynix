#! /usr/bin/env python3

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from subprocess import PIPE
from typing import TYPE_CHECKING

from anyio import TemporaryDirectory
import structlog

from pynixd.config import LocalSocketStoreSpec
from pynixd.derived_path import DerivedPath
from pynixd.drv_parser import Derivation, read_drv_file
from pynixd.goals.goal import Goal, GoalContext
from pynixd.goals.manager import GoalManager
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from pynixd.system_features import KNOWN_FEATURES
from pynixd.types import StoreId
from pynixd.substitution import HttpBinaryCacheSubstituter, Substituter, SubstitutionManager

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

        ctx = GoalContext(
            goal_manager=GoalManager(),
            store=store,
            substitution_manager=SubstitutionManager(
                substituters=[HttpBinaryCacheSubstituter("https://cache.nixos.org")]
            ),
        )

        proc = await asyncio.create_subprocess_exec(
            "nix",
            "eval",
            "--raw",
            "--store",
            td,
            "--file",
            "tests/nix",
            "ca.depends_on_ca.drvPath",
            stdout=PIPE,
        )
        stdout, _ = await proc.communicate()
        drv_str = stdout.decode().splitlines()[0]
        log.info("drv_str", drv_str=drv_str)
        goal = Goal(derived_path=DerivedPath(f"{drv_str}!out"), ctx=ctx)
        await goal.execute()
        log.info("result", result=goal.result)
        await ctx.substitution_manager.close()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
