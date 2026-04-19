"""Pydantic models for the PYNIXD_CONFIG JSON file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .store import Store


class LocalSocketStoreSpec(BaseModel):
    type: Literal["local-socket"] = "local-socket"
    id: str | None = None
    store_path: Path = Path("/")
    socket_path: Path | None = None
    max_builds: int = 1
    max_transfers: int = 4
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    nix_bin: str = "nix"
    extra_env: dict[str, str] | None = None
    extra_args: list[str] | None = None
    use_db: bool = True

    def to_store(self) -> Store:
        from .store import LocalSocketStore

        return LocalSocketStore(
            id=self.id,
            store_path=self.store_path,
            socket_path=self.socket_path,
            max_builds=self.max_builds,
            max_transfers=self.max_transfers,
            systems=self.systems,
            system_features=self.system_features,
            nix_bin=self.nix_bin,
            extra_env=self.extra_env,
            extra_args=self.extra_args,
            use_db=self.use_db,
        )


class LocalSubprocessStoreSpec(BaseModel):
    type: Literal["local-subprocess"] = "local-subprocess"
    id: str | None = None
    store_path: Path
    max_builds: int = 1
    max_transfers: int = 4
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    nix_bin: str = "nix"
    extra_env: dict[str, str] | None = None
    extra_args: list[str] | None = None
    use_db: bool = True

    def to_store(self) -> Store:
        from .store import LocalSocketStore

        return LocalSocketStore(
            id=self.id,
            store_path=self.store_path,
            max_builds=self.max_builds,
            max_transfers=self.max_transfers,
            systems=self.systems,
            system_features=self.system_features,
            nix_bin=self.nix_bin,
            extra_env=self.extra_env,
            extra_args=self.extra_args,
            use_db=self.use_db,
        )


class SSHSubprocessStoreSpec(BaseModel):
    type: Literal["ssh-subprocess"] = "ssh-subprocess"
    host: str
    id: str | None = None
    port: int = 22
    username: str | None = None
    store_path: Path = Path("/")
    max_builds: int = 2
    max_transfers: int = 4
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    monitor: bool = True
    nix_bin: str = "nix"

    def to_store(self) -> Store:
        from .store import SSHSubprocessStore

        return SSHSubprocessStore(
            host=self.host,
            id=self.id,
            port=self.port,
            username=self.username,
            store_path=self.store_path,
            max_builds=self.max_builds,
            max_transfers=self.max_transfers,
            systems=self.systems,
            system_features=self.system_features,
            monitor=self.monitor,
            nix_bin=self.nix_bin,
        )


class SSHSocketStoreSpec(BaseModel):
    type: Literal["ssh-socket"] = "ssh-socket"
    host: str
    id: str | None = None
    port: int = 22
    username: str | None = None
    socket_path: Path = Path("/nix/var/nix/daemon-socket/socket")
    max_builds: int = 2
    max_transfers: int = 4
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    monitor: bool = True

    def to_store(self) -> Store:
        from .store import SSHSocketStore

        return SSHSocketStore(
            host=self.host,
            id=self.id,
            port=self.port,
            username=self.username,
            socket_path=self.socket_path,
            max_builds=self.max_builds,
            max_transfers=self.max_transfers,
            systems=self.systems,
            system_features=self.system_features,
            monitor=self.monitor,
        )


StoreSpec = Annotated[
    LocalSocketStoreSpec
    | LocalSubprocessStoreSpec
    | SSHSubprocessStoreSpec
    | SSHSocketStoreSpec,
    Field(discriminator="type"),
]


class PynixdConfigFile(BaseModel):
    """Top-level model for the PYNIXD_CONFIG JSON file."""

    stores: list[StoreSpec] = Field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> PynixdConfigFile:
        with open(path) as f:
            data = json.load(f)
        return cls.model_validate(data)
