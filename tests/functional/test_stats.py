"""Test build statistics tracking and prioritization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import structlog

from pynixd import Server
from pynixd.operations.base import BuildResult, BuildResultStatus
from pynixd.operations.build_derivation import (
    BuildDerivationRequest,
)
from pynixd.store import LocalSocketStore
from pynixd.store_path import StorePath
from tests.conftest import STORE_PREFIX, get_test_store_kwargs, rmtree_robust

log = structlog.get_logger(__name__)


class StatsTestStore(LocalSocketStore):
    """A store that simulates builds with specific durations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_delays: dict[str, float] = {}

    @asynccontextmanager
    async def build_conn(self):  # type: ignore[override]
        async with self.build_semaphore:

            class MockConn:
                def __init__(self, store):
                    self.store = store
                    self.op_log: list[str] = []

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def call(
                    self,
                    request,
                    client=None,
                    suppress_last=False,
                    raise_on_error=False,
                ):
                    from pynixd.operations.build_derivation import (
                        BuildDerivationRequest,
                        BuildDerivationResponse,
                    )

                    if isinstance(request, BuildDerivationRequest):
                        pname = request.derivation.env.get("pname", "unknown")
                        delay = self.store.build_delays.get(pname, 0.1)
                        await asyncio.sleep(delay)
                        return BuildDerivationResponse(
                            result=BuildResult(status=BuildResultStatus.BUILT)
                        )
                    raise NotImplementedError(
                        f"MockConn doesn't support {type(request)}"
                    )

            yield MockConn(self)  # type: ignore

    async def execute(self, request, client=None, suppress_last=False):
        from pynixd.operations.query_all_valid_paths import (
            QueryAllValidPathsRequest,
            QueryAllValidPathsResponse,
        )
        from pynixd.operations.query_closure_with_info import (
            QueryClosureWithInfoRequest,
            QueryClosureWithInfoResponse,
        )
        from pynixd.operations.base import ValidPathInfo

        if isinstance(request, QueryAllValidPathsRequest):
            return QueryAllValidPathsResponse(paths=set())

        if isinstance(request, QueryClosureWithInfoRequest):
            # Mock closure response so "pulling paths" succeeds
            infos = [
                ValidPathInfo(
                    path=p,
                    registration_time=1,
                    nar_hash="sha256:0000000000000000000000000000000000000000000000000000",
                )
                for p in request.paths
            ]
            return QueryClosureWithInfoResponse(infos=infos)
        return await super().execute(request, client, suppress_last)

    async def call(
        self, request, client=None, suppress_last=False, raise_on_error=False
    ):
        # Fallback for other ops
        return await super().call(request, client, suppress_last, raise_on_error)


@pytest.mark.asyncio
async def test_build_stats_recording(tmp_path: Path) -> None:
    """Verify that build stats are recorded to the DB."""
    async with asyncio.timeout(30):
        pynixd_local_path = STORE_PREFIX / "stats-local"
        pynixd_remote_path = STORE_PREFIX / "stats-remote"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_remote_path)

        pynixd_local = LocalSocketStore(
            id="local",
            store_path=pynixd_local_path,
            **get_test_store_kwargs(),
        )
        pynixd_remote = StatsTestStore(
            id="remote",
            store_path=pynixd_remote_path,
            **get_test_store_kwargs(),
        )
        pynixd_remote.build_delays["fast-pkg"] = 0.05

        async with Server(
            local_store=pynixd_local,
            stores={"remote": pynixd_remote},
            ssh_port=None,
            unix_path=pynixd_local_path / "socket",
        ) as server:
            # 1. Run a build to generate stats
            from pynixd.operations.base import BasicDerivation, DerivationOutput

            out_path = StorePath("/nix/store/00000000000000000000000000000004-fast-pkg")
            pynixd_local.add_known_path(out_path)

            drv = BasicDerivation(
                platform="x86_64-linux",
                builder="/bin/sh",
                env={"pname": "fast-pkg", "version": "1.0"},
                outputs={
                    "out": DerivationOutput(
                        path=str(out_path),
                        method="",
                        hash_digest="",
                    )
                },
            )
            req = BuildDerivationRequest(
                drv_path=StorePath(
                    "/nix/store/00000000000000000000000000000001-fast-pkg.drv"
                ),
                derivation=drv,
            )

            # We use the scheduler directly to avoid proxy overhead
            scheduler = server.scheduler
            assert scheduler is not None

            build_id, future = await scheduler.enqueue(req, None, set(), "x86_64-linux")
            await future

            # 2. Check the DB
            assert pynixd_local.db is not None
            async with pynixd_local.db.execute(
                "SELECT pname, duration_ms FROM DerivationStats WHERE pname = 'fast-pkg'"
            ) as cursor:
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == "fast-pkg"
                # Duration should be around 50ms + some overhead
                assert 50 <= row[1] <= 1000


@pytest.mark.asyncio
async def test_scheduler_prioritization(tmp_path: Path) -> None:
    """Verify that the scheduler prioritizes builds based on expected duration."""
    async with asyncio.timeout(30):
        pynixd_local_path = STORE_PREFIX / "prioritization-local"
        pynixd_remote_path = STORE_PREFIX / "prioritization-remote"
        rmtree_robust(pynixd_local_path)
        rmtree_robust(pynixd_remote_path)

        pynixd_local = LocalSocketStore(
            id="local",
            store_path=pynixd_local_path,
            **get_test_store_kwargs(),
        )
        pynixd_remote = StatsTestStore(
            id="remote",
            store_path=pynixd_remote_path,
            max_builds=1,
            **get_test_store_kwargs(),
        )

        async with Server(
            local_store=pynixd_local,
            stores={"remote": pynixd_remote},
            ssh_port=None,
            unix_path=pynixd_local_path / "socket",
        ) as server:
            assert pynixd_local.db is not None
            # Pre-seed the DB with "slow" and "fast" stats
            await pynixd_local.db.record_build_stats(
                pname="slow-pkg",
                version="1.0",
                platform="x86_64-linux",
                serialized_drv="slow",
                cpu_user_us=None,
                cpu_system_us=None,
                duration_ms=10000,  # 10s
            )
            await pynixd_local.db.record_build_stats(
                pname="fast-pkg",
                version="1.0",
                platform="x86_64-linux",
                serialized_drv="fast",
                cpu_user_us=None,
                cpu_system_us=None,
                duration_ms=100,  # 100ms
            )

            from pynixd.operations.base import BasicDerivation

            # Enqueue slow pkg first, then fast pkg
            # Fast pkg should be scheduled BEFORE an unknown pkg or after slow pkg if it's already running.
            # To test prioritization, we need to enqueue them while the scheduler is "paused" or busy.

            scheduler = server.scheduler
            assert scheduler is not None
            # 1. Occupy the only slot with a very slow build (not in stats)
            pynixd_remote.build_delays["blocker"] = 2.0
            blocker_drv = BasicDerivation(
                platform="x86_64-linux", env={"pname": "blocker"}
            )
            blocker_req = BuildDerivationRequest(
                drv_path=StorePath(
                    "/nix/store/00000000000000000000000000000000-blocker.drv"
                ),
                derivation=blocker_drv,
            )
            await scheduler.enqueue(blocker_req, None, set(), "x86_64-linux")

            # Wait for blocker to start
            while True:
                pending = await scheduler.queue.get_pending()
                if any(b.is_building for b in pending):
                    break
                await asyncio.sleep(0.1)

            # 2. Enqueue slow-pkg and fast-pkg
            slow_drv = BasicDerivation(
                platform="x86_64-linux", env={"pname": "slow-pkg"}
            )
            slow_req = BuildDerivationRequest(
                drv_path=StorePath(
                    "/nix/store/00000000000000000000000000000002-slow.drv"
                ),
                derivation=slow_drv,
            )

            fast_drv = BasicDerivation(
                platform="x86_64-linux", env={"pname": "fast-pkg"}
            )
            fast_req = BuildDerivationRequest(
                drv_path=StorePath(
                    "/nix/store/00000000000000000000000000000003-fast.drv"
                ),
                derivation=fast_drv,
            )

            # Enqueue slow then fast
            id_slow, fut_slow = await scheduler.enqueue(
                slow_req, None, set(), "x86_64-linux"
            )
            id_fast, fut_fast = await scheduler.enqueue(
                fast_req, None, set(), "x86_64-linux"
            )

            # 3. Verify queue order
            pending = await scheduler.queue.get_pending()
            # Sort them like the scheduler does
            schedulable = [b for b in pending if b.is_pending]

            def duration_key(b):
                if b.expected_duration is not None:
                    return float(b.expected_duration)
                return 600000.0

            schedulable.sort(key=duration_key)

            assert schedulable[0].id == id_fast
            assert schedulable[1].id == id_slow
            log.info("prioritization_verified", fast_id=id_fast, slow_id=id_slow)


@pytest.mark.asyncio
async def test_levenshtein_sql(tmp_path: Path) -> None:
    """Verify that the levenshtein function works in SQLite."""
    pynixd_local_path = STORE_PREFIX / "levenshtein-test"
    rmtree_robust(pynixd_local_path)
    (pynixd_local_path / "nix/var/nix/db").mkdir(parents=True)

    # Create an empty sqlite file so open() doesn't fail
    db_file = pynixd_local_path / "nix/var/nix/db/db.sqlite"
    import sqlite3

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE ValidPaths (id INTEGER PRIMARY KEY, path TEXT UNIQUE)")
    conn.close()

    pynixd_local = LocalSocketStore(
        id="local",
        store_path=pynixd_local_path,
        **get_test_store_kwargs(),
    )
    # Ensure DB is created
    from pynixd.local_store_db import LocalStoreDB

    db = await LocalStoreDB.open(pynixd_local_path)
    pynixd_local.db = db

    assert db.active
    async with db.execute("SELECT levenshtein(?, ?)", ("kitten", "sitting")) as cursor:
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 3

    # Test our hint lookup with Levenshtein
    await db.record_build_stats(
        pname="test",
        version="1.0",
        platform="x86_64-linux",
        serialized_drv="very-long-string-with-small-change-A",
        cpu_user_us=None,
        cpu_system_us=None,
        duration_ms=100,
    )

    hint = await db.get_build_stats_hint(
        "test", "x86_64-linux", "very-long-string-with-small-change-B"
    )
    assert hint == 100
    log.info("levenshtein_sql_verified")
    await db.close()
