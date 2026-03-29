"""Entry point: python -m pynixd

All configuration is via environment variables:

SSH Server:
  PYNIXD_HOST          Listen address (default: 127.0.0.1)
  PYNIXD_PORT          Listen port (default: 2234)
  PYNIXD_HOST_KEY      Path to SSH host key (generated if absent)
  PYNIXD_BACKEND_FILE  JSON file containing backend definitions
  PYNIXD_DEV           Dev mode: spawn N local builders (default: 0)
  PYNIXD_LOG_LEVEL     Log level: DEBUG, INFO, WARNING, ERROR (default: WARNING)

HTTP Binary Cache:
  PYNIXD_HTTP_PORT     HTTP binary cache port (0 to disable, default: 0)
  PYNIXD_HTTP_HOST     HTTP listen address (default: 0.0.0.0)
  PYNIXD_HTTP_USER     HTTP basic auth username
  PYNIXD_HTTP_PASS     HTTP basic auth password
  PYNIXD_HTTP_SSL_CERT TLS certificate path
  PYNIXD_HTTP_SSL_KEY  TLS private key path
  PYNIXD_HTTP_PRIORITY Binary cache priority (default: 30)

See also: gc.py, psi.py, scheduler.py for additional env vars.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from .http_cache import BinaryCacheServer
from .ssh_server import run_server
from .store import (
    LocalSocketStore,
    LocalSubprocessStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)


def _load_backends_from_file(path: str) -> dict[str, Store]:
    """Load store definitions from a JSON file.

    JSON format:
    [
        {
            "type": "ssh-subprocess",
            "host": "example.com",
            "port": 22,
            "username": "user",
            "id": "my-builder",
            "max_builds": 4,
            "supported_systems": ["x86_64-linux", "aarch64-linux"]
        },
        {
            "type": "local-socket",
            "socket_path": "/nix/var/nix/daemon-socket/socket",
            "id": "local",
            "max_builds": 1
        }
    ]

    Store types:
    - ssh-subprocess: SSH with nix-daemon --stdio
    - ssh-socket: SSH tunnel to remote Unix socket
    - local-socket: Local daemon via Unix socket
    - local-subprocess: Local nix-daemon subprocess
    """
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

        local_store = LocalSocketStore(id="local")

    cache_runner = None
    http_port = _env_int("PYNIXD_HTTP_PORT", 0)
    try:
        if http_port:
            cache = BinaryCacheServer(
                local_store,
                username=_env("PYNIXD_HTTP_USER") or None,
                password=_env("PYNIXD_HTTP_PASS") or None,
                priority=_env_int("PYNIXD_HTTP_PRIORITY", 30),
            )
            cache_runner, _ = await cache.start(
                host=_env("PYNIXD_HTTP_HOST") or "0.0.0.0",
                port=http_port,
                ssl_cert=_env("PYNIXD_HTTP_SSL_CERT") or None,
                ssl_key=_env("PYNIXD_HTTP_SSL_KEY") or None,
            )

        await run_server(
            stores=stores,
            local_store=local_store,
            host=_env("PYNIXD_HOST") or "127.0.0.1",
            port=_env_int("PYNIXD_PORT", 2234),
            host_key_path=_env("PYNIXD_HOST_KEY") or None,
        )
    finally:
        if cache_runner is not None:
            await cache_runner.cleanup()
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
        logging.getLogger(__name__).info("Shutting down")


if __name__ == "__main__":
    main()
