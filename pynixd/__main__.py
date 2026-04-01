"""Entry point: python -m pynixd

All configuration is via environment variables:

General:
  PYNIXD_HOST          Listen address for SSH (default: 127.0.0.1)
  PYNIXD_BACKEND_FILE  JSON file containing backend definitions
  PYNIXD_DEV           Dev mode: spawn N local builders (default: 0)
  PYNIXD_LOG_LEVEL     Log level: DEBUG, INFO, WARNING, ERROR (default: WARNING)

SSH Server:
  PYNIXD_SSH_PORT      SSH listen port (default: 2234 if PYNIXD_PORT is set,
                       else 0 to disable)
  PYNIXD_PORT          Legacy SSH listen port (alias for PYNIXD_SSH_PORT)
  PYNIXD_HOST_KEY      Path to SSH host key (generated if absent)

Unix Server:
  PYNIXD_UNIX_PATH     Path for Unix domain socket (disabled if empty)

HTTP Binary Cache:
  PYNIXD_HTTP_PORT     HTTP binary cache port (0 to disable, default: 0)
  PYNIXD_HTTP_HOST     HTTP listen address (default: 0.0.0.0)
  PYNIXD_HTTP_USER     HTTP basic auth username
  PYNIXD_HTTP_PASS     HTTP basic auth password
  PYNIXD_HTTP_PRIORITY Binary cache priority (default: 30)

HTTPS Binary Cache:
  PYNIXD_HTTPS_PORT    HTTPS binary cache port (0 to disable, default: 0)
  PYNIXD_HTTPS_CERT    TLS certificate path
  PYNIXD_HTTPS_KEY     TLS private key path

See also: gc.py, psi.py, scheduler.py for additional env vars.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from .build_queue import BuildQueue
from .gc import GarbageCollector
from .http_cache import BinaryCacheServer
from .local_store_db import LocalStoreDB
from .scheduler import Scheduler
from .ssh_server import start_ssh_server
from .store import (
    LocalSocketStore,
    LocalSubprocessStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)
from .unix_server import start_unix_server


def _load_backends_from_file(path: str) -> dict[str, Store]:
    """Load store definitions from a JSON file."""
    with open(path) as f:
        data = json.load(f)

    stores: dict[str, Store] = {}
    for spec in data:
        btype = spec.get("type")
        b_id = spec.get("id")
        max_builds = spec.get("max_builds", 2)
        max_transfers = spec.get("max_transfers", 4)
        supported_systems = spec.get("supported_systems")

        if btype == "ssh-subprocess":
            store = SSHSubprocessStore(
                host=spec["host"],
                id=b_id,
                port=spec.get("port", 22),
                username=spec.get("username"),
                store_path=spec.get("store_path"),
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        elif btype == "ssh-socket":
            store = SSHSocketStore(
                host=spec["host"],
                id=b_id,
                port=spec.get("port", 22),
                username=spec.get("username"),
                socket_path=spec.get(
                    "socket_path", "/nix/var/nix/daemon-socket/socket"
                ),
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        elif btype == "local-socket":
            store = LocalSocketStore(
                id=b_id,
                store_path=spec.get("store_path", "/"),
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        elif btype == "local-subprocess":
            store = LocalSubprocessStore(
                store_path=spec["store_path"],
                id=b_id,
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        else:
            raise ValueError(f"Unknown store type: {btype!r}")

        stores[store.id] = store

    return stores


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw else default


async def _async_main() -> None:
    dev_mode = _env_int("PYNIXD_DEV", 0)
    stores: dict[str, Store] = {}
    log_main: logging.Logger = logging.getLogger("pynixd")

    if dev_mode > 0:
        log_main.info("Development mode: %d local builders", dev_mode)

        for i in range(dev_mode):
            store = LocalSubprocessStore(
                store_path=f"/tmp/pynixd-{i}",
                id=f"builder{i}",
                max_builds=2,
            )
            stores[store.id] = store

        local_store: Store = LocalSubprocessStore(
            store_path="/tmp/pynixdlocal",
            id="local",
        )

    else:
        backend_file = _env("PYNIXD_BACKEND_FILE")
        if backend_file:
            if not os.path.exists(backend_file):
                raise FileNotFoundError(f"Backend file not found: {backend_file}")
            log_main.info("Loading stores from %s", backend_file)
            stores = _load_backends_from_file(backend_file)
        else:
            raise ValueError("PYNIXD_BACKEND_FILE is required in non-dev mode")

        if "local" in stores:
            local_store = stores.pop("local")
            log_main.info("Using 'local' from backend file as local store")
        else:
            local_store = LocalSocketStore(id="local")

    # Shared resources
    build_queue = BuildQueue()
    scheduler = Scheduler(build_queue, stores, local_store)

    # Initialize local store and backends
    await local_store.probe_version()
    local_store.db = await LocalStoreDB.open(local_store.store_path or "/")

    for store in stores.values():
        try:
            await store.sync_paths()
        except Exception:
            log_main.exception("Failed to sync paths for store %s", store.id)

    background_tasks = []
    servers = []
    http_runners = []

    try:
        # Start background services
        background_tasks.append(asyncio.create_task(scheduler.start()))
        if local_store.db:
            local_store.db.start()
            gc = GarbageCollector(local_store.db, stores, local_store)
            background_tasks.append(asyncio.create_task(gc._loop()))

        # Start listeners
        ssh_port = _env_int("PYNIXD_SSH_PORT", _env_int("PYNIXD_PORT", 0))
        if ssh_port:
            ssh_server = await start_ssh_server(
                stores=stores,
                local_store=local_store,
                build_queue=build_queue,
                scheduler=scheduler,
                host=_env("PYNIXD_HOST") or "127.0.0.1",
                port=ssh_port,
                host_key_path=_env("PYNIXD_HOST_KEY") or None,
            )
            servers.append(ssh_server)

        unix_path = _env("PYNIXD_UNIX_PATH")
        if unix_path:
            unix_server = await start_unix_server(
                stores=stores,
                local_store=local_store,
                build_queue=build_queue,
                scheduler=scheduler,
                socket_path=unix_path,
            )
            servers.append(unix_server)

        http_port = _env_int("PYNIXD_HTTP_PORT", 0)
        https_port = _env_int("PYNIXD_HTTPS_PORT", 0)
        if http_port or https_port:
            cache = BinaryCacheServer(
                local_store,
                username=_env("PYNIXD_HTTP_USER") or None,
                password=_env("PYNIXD_HTTP_PASS") or None,
                priority=_env_int("PYNIXD_HTTP_PRIORITY", 30),
            )
            if http_port:
                runner, _ = await cache.start(
                    host=_env("PYNIXD_HTTP_HOST") or "0.0.0.0",
                    port=http_port,
                )
                http_runners.append(runner)
            if https_port:
                runner, _ = await cache.start(
                    host=_env("PYNIXD_HTTP_HOST") or "0.0.0.0",
                    port=https_port,
                    ssl_cert=_env("PYNIXD_HTTPS_CERT") or None,
                    ssl_key=_env("PYNIXD_HTTPS_KEY") or None,
                )
                http_runners.append(runner)

        if not servers and not http_runners:
            log_main.warning("No servers started! Check your configuration.")
            return

        # Wait for all servers to close
        wait_tasks = []
        for s in servers:
            if hasattr(s, "wait_closed"):
                wait_tasks.append(asyncio.create_task(s.wait_closed()))

        # AppRunners don't have a wait_closed that blocks until the server stops
        # in the same way as asyncio.Server. They just run until cleaned up.
        # So we just wait on the asyncio.Servers if any, or a long sleep.
        if wait_tasks:
            await asyncio.gather(*wait_tasks)
        else:
            # Only HTTP runners, wait forever
            while True:
                await asyncio.sleep(3600)

    except asyncio.CancelledError:
        log_main.info("Shutting down...")
    finally:
        for runner in http_runners:
            await runner.cleanup()
        for s in servers:
            s.close()
        for task in background_tasks:
            task.cancel()

        await local_store.close()
        for store in stores.values():
            await store.close()


def main() -> None:
    log_level_str = _env("PYNIXD_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, log_level_str, logging.WARNING)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
