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
    from .store import (
        Store,
    )


def _feature_matrix_from_config(
    systems: set[str] | None,
    system_features: set[str],
) -> dict[str, set[str]] | None:
    """Convert config-level systems + system_features into a feature_matrix.

    Returns None if both are empty/default (meaning: probe at startup).
    """
    if systems and system_features:
        return {s: set(system_features) for s in systems}
    if systems:
        return {s: set() for s in systems}
    if system_features:
        raise ValueError(
            "system_features specified without systems; specify systems to create a feature_matrix",
        )
    return None


class StoreRankingSettings(BaseModel):
    """Configurable weights for the telemetry-driven store ranking algorithm."""

    locality_weight: float = 500.0
    """Points for data locality: (common_paths / total_paths) * locality_weight."""

    cpu_idle_weight: float = 100.0
    """Points for CPU availability: (1.0 - cpu_utilization) * cpu_idle_weight."""

    cpu_pressure_penalty: float = 50.0
    """Penalty scaled by CPU PSI average: -(cpu_psi * cpu_pressure_penalty)."""

    io_pressure_penalty: float = 50.0
    """Penalty scaled by IO PSI average: -(io_psi * io_pressure_penalty)."""

    concurrency_penalty: float = 50.0
    """Penalty per active connection: -(active_conns * concurrency_penalty)."""

    predicted_load_penalty_per_min: float = 10.0
    """Penalty per expected minute of in-flight work: -(predicted_mins * load_penalty)."""

    thundering_herd_penalty: float = 100.0
    """Penalty per build assigned in current scheduling pass: -(assigned * penalty)."""

    min_schedule_score: float = 0.0
    """Minimum score required for a store to be considered. If lower, builds stay queued."""


class LocalSocketStoreSpec(BaseModel):
    type: Literal["local-socket"] = "local-socket"
    id: str | None = None
    store_path: Path = Path("/")
    socket_path: Path | None = None
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    nix_bin: str = "nix"
    extra_env: dict[str, str] | None = None
    extra_args: list[str] | None = None
    use_db: bool = True

    def to_store(self) -> Store:
        from .store import LocalSocketStore

        feature_matrix = _feature_matrix_from_config(self.systems, self.system_features)
        return LocalSocketStore(
            id=self.id,
            store_path=self.store_path,
            socket_path=self.socket_path,
            feature_matrix=feature_matrix,
            probe=feature_matrix is None,
            nix_bin=self.nix_bin,
            extra_env=self.extra_env,
            extra_args=self.extra_args,
            use_db=self.use_db,
        )


class LocalSubprocessStoreSpec(BaseModel):
    type: Literal["local-subprocess"] = "local-subprocess"
    id: str | None = None
    store_path: Path
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    nix_bin: str = "nix"
    extra_env: dict[str, str] | None = None
    extra_args: list[str] | None = None
    use_db: bool = True

    def to_store(self) -> Store:
        from .store import LocalSocketStore

        feature_matrix = _feature_matrix_from_config(self.systems, self.system_features)
        return LocalSocketStore(
            id=self.id,
            store_path=self.store_path,
            feature_matrix=feature_matrix,
            probe=feature_matrix is None,
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
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    monitor: bool = True
    nix_bin: str = "nix"

    def to_store(self) -> Store:
        from .store import SSHSubprocessStore

        feature_matrix = _feature_matrix_from_config(self.systems, self.system_features)
        return SSHSubprocessStore(
            host=self.host,
            id=self.id,
            port=self.port,
            username=self.username,
            store_path=self.store_path,
            feature_matrix=feature_matrix,
            probe=feature_matrix is None,
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
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    monitor: bool = True

    def to_store(self) -> Store:
        from .store import SSHSocketStore

        feature_matrix = _feature_matrix_from_config(self.systems, self.system_features)
        return SSHSocketStore(
            host=self.host,
            id=self.id,
            port=self.port,
            username=self.username,
            socket_path=self.socket_path,
            feature_matrix=feature_matrix,
            probe=feature_matrix is None,
            monitor=self.monitor,
        )


StoreSpec = Annotated[
    LocalSocketStoreSpec | LocalSubprocessStoreSpec | SSHSubprocessStoreSpec | SSHSocketStoreSpec,
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
    http_enable_cache: bool = True
    http_enable_metrics: bool = True
    http_metrics_no_auth: bool = True
    http_user: str | None = None
    http_pass: str | None = None
    http_htpasswd: Path | None = None
    http_priority: int = 30
    http_upload_dir: Path | None = None

    https_port: int | None = None
    https_cert: Path | None = None
    https_key: Path | None = None

    admin_users: set[str] = Field(default_factory=set)

    idle_timeout: int | None = None

    gc_interval: float = 3600.0
    gc_local_max_age: int = 604800
    gc_builder_max_age: int = 3600

    # Scheduling & Telemetry
    ranking: StoreRankingSettings = Field(default_factory=StoreRankingSettings)

    # Resource Monitoring
    psi_cpu_threshold: float = 15.0  # % pressure (some)
    psi_mem_threshold: float = 10.0  # % pressure (some)
    psi_io_threshold: float = 10.0  # % pressure (some)
    min_available_memory_mb: int = 512  # Hard gate: block if < 512MB available
    max_cpu_util: float = 90.0  # Fallback: max 90% utilization
    gate_timeout: float = 5.0  # seconds to wait for pressure to subside

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

        local_store = stores.pop("local") if "local" in stores else LocalSocketStore(id="local", store_path=Path("/"))

        return local_store, stores
