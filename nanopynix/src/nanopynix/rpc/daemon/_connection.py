"""Exec entrypoint for exactly one native Nix daemon connection."""

from __future__ import annotations

import argparse
import os

from nanopynix_bindings import main as nanopynix_main
from nanopynix_bindings import store as nanopynix_store
from nanopynix_bindings import util as nanopynix_util

from nanopynix.models import NIX_USER_CONF_FILES_ENV
from nanopynix.rpc.daemon._config import DaemonConfig


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 65_536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def run_connection(*, connection_fd: int, config_fd: int) -> None:
    """Initialise Nix and serve one inherited Unix connection descriptor."""
    config = DaemonConfig.from_bytes(_read_all(config_fd))
    if config.nix_conf is not None:
        os.environ[NIX_USER_CONF_FILES_ENV] = config.nix_conf
    for name, value in config.settings.items():
        nanopynix_util.set_setting(name, value)
    nanopynix_main.init_nix(load_config=config.load_config)
    store = nanopynix_store.open_store(config.store_uri)
    try:
        nanopynix_store.process_connection(store, connection_fd, trusted=config.trusted)
    finally:
        store.close()
        os.close(connection_fd)


def main() -> None:
    """Run one process-private daemon connection worker."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-fd", type=int, required=True)
    parser.add_argument("--config-fd", type=int, required=True)
    args = parser.parse_args()
    run_connection(connection_fd=args.connection_fd, config_fd=args.config_fd)


if __name__ == "__main__":
    main()
