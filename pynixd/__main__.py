"""Entry point: python -m pynixd

All configuration is via environment variables:

General:
  PYNIXD_HOST          Listen address for SSH (default: 127.0.0.1)
  PYNIXD_BACKEND_FILE  JSON file containing backend definitions
  PYNIXD_DEV           Dev mode: spawn N local builders (default: 0)
  PYNIXD_LOG_LEVEL     Log level: DEBUG, INFO, WARNING, ERROR (default: WARNING)

SSH Server:
  PYNIXD_SSH_PORT      SSH listen port (default: 2234, 0 to disable)
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
from pathlib import Path

import structlog
from environs import Env

from .instance import PynixdConfig, Server
from .store import (
    LocalSocketStore,
    SSHSocketStore,
    SSHSubprocessStore,
    Store,
)

env = Env()
log = structlog.get_logger(__name__)

env = Env()


def load_backends_from_file(path: Path) -> dict[str, Store]:
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
                store_path=Path(spec["store_path"]) if spec.get("store_path") else None,
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
                socket_path=Path(
                    spec.get("socket_path", "/nix/var/nix/daemon-socket/socket")
                ),
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        elif btype == "local-socket":
            store = LocalSocketStore(
                id=b_id,
                store_path=Path(spec.get("store_path", "/")),
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        elif btype == "local-subprocess":
            store = LocalSocketStore(
                store_path=Path(spec["store_path"]),
                id=b_id,
                max_builds=max_builds,
                max_transfers=max_transfers,
                supported_systems=supported_systems,
            )
        else:
            raise ValueError(f"Unknown store type: {btype!r}")

        stores[store.id] = store

    return stores


async def async_main() -> None:
    dev_mode = env.int("PYNIXD_DEV", 0)
    stores: dict[str, Store] = {}

    if dev_mode > 0:
        log.info("dev_mode", count=dev_mode)

        for i in range(dev_mode):
            store = LocalSocketStore(
                store_path=Path(f"/tmp/pynixd-{i}"),
                id=f"builder{i}",
                max_builds=2,
            )
            stores[store.id] = store

        local_store: Store = LocalSocketStore(
            store_path=Path("/tmp/pynixdlocal"),
            id="local",
        )

    else:
        backend_file = env.path("PYNIXD_BACKEND_FILE", None)
        if backend_file:
            if not backend_file.exists():
                raise FileNotFoundError(f"Backend file not found: {backend_file}")
            log.info("loading_backends", path=str(backend_file))
            stores = load_backends_from_file(backend_file)
        else:
            raise ValueError("PYNIXD_BACKEND_FILE is required in non-dev mode")

        if "local" in stores:
            local_store = stores.pop("local")
            log.info("using_local_from_backend")
        else:
            local_store = LocalSocketStore(id="local", store_path=Path("/"))

    config = PynixdConfig(
        local_store=local_store,
        stores=stores,
        ssh_host=env.str("PYNIXD_HOST", "127.0.0.1"),
        ssh_port=env.int("PYNIXD_SSH_PORT", 2234),
        ssh_host_key=env.path("PYNIXD_HOST_KEY", None),
        unix_path=env.path("PYNIXD_UNIX_PATH", None),
        http_host=env.str("PYNIXD_HTTP_HOST", "0.0.0.0"),
        http_port=env.int("PYNIXD_HTTP_PORT", None),
        http_user=env.str("PYNIXD_HTTP_USER", None),
        http_pass=env.str("PYNIXD_HTTP_PASS", None),
        http_priority=env.int("PYNIXD_HTTP_PRIORITY", 30),
        https_port=env.int("PYNIXD_HTTPS_PORT", None),
        https_cert=env.path("PYNIXD_HTTPS_CERT", None),
        https_key=env.path("PYNIXD_HTTPS_KEY", None),
    )

    server = Server(config)
    try:
        await server.start()
        await server.wait_finished()
    finally:
        await server.close()


def main() -> None:
    log_level_str = env.str("PYNIXD_LOG_LEVEL", "WARNING").upper()

    logging.basicConfig(
        level=log_level_str,
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
