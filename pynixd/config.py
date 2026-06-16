"""Pydantic-settings model for the PYNIXD_CONFIG JSON file and env vars."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .types.ids import StoreId


class ScheduleMode(StrEnum):
    auto = "auto"
    proxy = "proxy"
    scheduler = "scheduler"


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


class StoreSpecBase(BaseModel):
    """Common settings shared by all store specs."""

    store_id: StoreId | None = None
    systems: set[str] | None = None
    system_features: set[str] = Field(default_factory=set)
    feature_matrix: dict[str, set[str]] | None = None
    nix_bin: str = "nix"
    idle_ttl: float = 10.0
    scheduleable: bool = True
    priority: float = 1.0
    score_penalty: int = 0
    gc_enabled: bool = True
    gc_max_age: int | None = None
    no_schedule: bool = False
    probe: bool | None = None
    settings: PynixdSettings | None = None
    reconnect: bool = True
    reconnect_min_delay: float = 1.0
    reconnect_max_delay: float = 300.0

    def _effective_feature_matrix(self) -> dict[str, set[str]] | None:
        if self.feature_matrix is not None:
            return self.feature_matrix
        return _feature_matrix_from_config(self.systems, self.system_features)

    def to_store(self, store_id: str) -> Store:
        raise NotImplementedError(f"{type(self).__name__} must implement to_store()")


class LocalSocketStoreSpec(StoreSpecBase):
    type: Literal["local-socket"] = "local-socket"
    store_path: Path = Path("/")
    socket_path: Path | None = None
    extra_env: dict[str, str] | None = None
    extra_args: list[str] | None = None
    use_db: bool = True
    monitor: bool = True

    def to_store(self, store_id: str) -> Store:
        from .store.local_daemon import LocalStore
        from .store.local_db import LocalDBStore

        spec = self.model_copy(update={"store_id": StoreId(store_id)})
        if spec.use_db:
            return LocalDBStore(spec)
        return LocalStore(spec)


class LocalSubprocessStoreSpec(StoreSpecBase):
    type: Literal["local-subprocess"] = "local-subprocess"
    store_path: Path  # Required — overrides parent default of /
    extra_env: dict[str, str] | None = None
    extra_args: list[str] | None = None
    use_db: bool = True
    monitor: bool = True
    socket_path: Path | None = None

    def to_store(self, store_id: str) -> Store:
        from .store.local_daemon import LocalStore
        from .store.local_db import LocalDBStore

        spec = self.model_copy(update={"store_id": StoreId(store_id)})
        if spec.use_db:
            return LocalDBStore(spec)
        return LocalStore(spec)


class ReverseStoreSpec(StoreSpecBase):
    """Configuration for a reverse store (builder connects to controller).

    Reverse stores are created dynamically when a builder registers with
    the controller via the reverse server. This spec exists to provide a
    type in the StoreSpec union and document the expected fields.
    """

    type: Literal["reverse"] = "reverse"

    def to_store(self, store_id: str) -> Store:
        raise NotImplementedError("Reverse stores are created dynamically, not from config")


class ReverseAcceptorSettings(BaseModel):
    """Settings for the reverse acceptor (controller side).

    The reverse acceptor listens for builder-initiated SSH connections.
    Builders (reverse initiators) register themselves as build stores,
    enabling NAT traversal.
    """

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 2235
    host_key_path: Path | None = None


class ReverseInitiatorSettings(BaseModel):
    """Settings for the reverse initiator (builder side).

    The reverse initiator connects to a controller's reverse acceptor and
    registers the local pynixd instance as a build store.  After registration,
    the controller opens pooled ``nix-daemon --stdio`` sessions to execute
    builds and queries.
    """

    enabled: bool = False
    acceptor_host: str = "127.0.0.1"
    acceptor_port: int = 2235
    store_id: str | None = None
    systems: list[str] | None = None
    system_features: list[str] = Field(default_factory=list)
    nix_bin: str = "nix"
    server_host_key_paths: list[Path] = Field(default_factory=list)
    reconnect_min_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    shutdown_on_connect_failure_seconds: float | None = None


class SSHSubprocessStoreSpec(StoreSpecBase):
    type: Literal["ssh-subprocess"] = "ssh-subprocess"
    host: str
    port: int = 22
    username: str | None = None
    store_path: Path = Path("/")
    monitor: bool = True
    client_keys: list[Any] = Field(default_factory=list)

    def to_store(self, store_id: str) -> Store:
        from .store import SSHSubprocessStore

        return SSHSubprocessStore(
            self.model_copy(update={"store_id": StoreId(store_id)}),
        )


class SSHSocketStoreSpec(StoreSpecBase):
    type: Literal["ssh-socket"] = "ssh-socket"
    host: str
    port: int = 22
    username: str | None = None
    socket_path: Path = Path("/nix/var/nix/daemon-socket/socket")
    monitor: bool = True
    client_keys: list[Any] = Field(default_factory=list)

    def to_store(self, store_id: str) -> Store:
        from .store import SSHSocketStore

        return SSHSocketStore(
            self.model_copy(update={"store_id": StoreId(store_id)}),
        )


StoreSpec = Annotated[
    LocalSocketStoreSpec | LocalSubprocessStoreSpec | SSHSubprocessStoreSpec | SSHSocketStoreSpec | ReverseStoreSpec,
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
        with path.open() as f:
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
    stores: dict[str, StoreSpec] = Field(default_factory=dict)

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

    reverse_acceptor: ReverseAcceptorSettings = Field(default_factory=ReverseAcceptorSettings)
    reverse_initiator: ReverseInitiatorSettings = Field(default_factory=ReverseInitiatorSettings)

    admin_users: set[str] = Field(default_factory=set)

    gc_enabled: bool = True
    gc_interval: float = 3600.0

    # Scheduling & Telemetry
    schedule_mode: ScheduleMode = ScheduleMode.auto
    ranking: StoreRankingSettings = Field(default_factory=StoreRankingSettings)

    # Resource Monitoring
    psi_cpu_threshold: float = 15.0  # % pressure (some)
    psi_mem_threshold: float = 10.0  # % pressure (some)
    psi_io_threshold: float = 10.0  # % pressure (some)
    min_available_memory_mb: int = 512  # Hard gate: block if < 512MB available
    max_cpu_util: float = 90.0  # Fallback: max 90% utilization
    gate_timeout: float = 5.0  # seconds to wait for pressure to subside

    # Plugins
    plugins: list[Path] = Field(default_factory=list)

    # Logging (the log-level threshold; filtering is handled by plugins)
    log_level: str = "WARNING"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:

        return (init_settings, env_settings, _ConfigFileSource(settings_cls))

    def to_stores(self) -> dict[StoreId, Store]:
        """Convert all store specs to live Store instances."""
        from .store import LocalStore

        stores: dict[StoreId, Store] = {}
        for key, spec in self.stores.items():
            spec.settings = self
            store = spec.to_store(store_id=key)
            stores[store.store_id] = store

        if StoreId("local") not in stores:
            spec = LocalSocketStoreSpec(
                store_id=StoreId("local"),
                monitor=False,
                settings=self,
            )
            stores[StoreId("local")] = LocalStore(spec)

        return stores
