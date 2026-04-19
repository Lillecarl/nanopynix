"""Pydantic-settings model for the PYNIXD_CONFIG JSON file and env vars."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

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


class _ConfigFileSource(PydanticBaseSettingsSource):
    """Read settings fields from a JSON config file (PYNIXD_CONFIG)."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        config_path = self.settings_cls.model_fields.get("config")
        if config_path is None:
            return {}
        env_val = self.current_state.get("config")
        if env_val is None:
            return {}
        path = Path(env_val)
        if not path.exists():
            return {}
        with open(path) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in self.settings_cls.model_fields}


class PynixdSettings(BaseSettings):
    """Unified configuration from env vars (priority) and config file.

    Env var mapping: PYNIXD_<FIELD_NAME> (e.g. PYNIXD_SSH_PORT → ssh_port).
    Config file fields (lower priority) are loaded from the JSON file at
    PYNIXD_CONFIG (if it points to an existing path).
    """

    model_config = SettingsConfigDict(env_prefix="PYNIXD_")

    config: Path | None = None
    stores: list[StoreSpec] = Field(default_factory=list)

    ssh_host: str = "127.0.0.1"
    ssh_port: int | None = 2234
    ssh_host_key: Path | None = None

    unix_path: Path | None = None

    http_host: str = "0.0.0.0"
    http_port: int | None = None
    http_user: str | None = None
    http_pass: str | None = None
    http_htpasswd: Path | None = None
    http_priority: int = 30
    http_upload_dir: Path | None = None

    https_port: int | None = None
    https_cert: Path | None = None
    https_key: Path | None = None

    admin_users: set[str] = Field(default_factory=set)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, _ConfigFileSource(settings_cls))

    def to_stores(self) -> tuple[Store, dict[str, Store]]:
        """Convert all store specs to live Store instances, separating out 'local'."""
        from .store import LocalSocketStore

        stores: dict[str, Store] = {}
        for spec in self.stores:
            store = spec.to_store()
            stores[store.id] = store

        if "local" in stores:
            local_store = stores.pop("local")
        else:
            local_store = LocalSocketStore(id="local", store_path=Path("/"))

        return local_store, stores
