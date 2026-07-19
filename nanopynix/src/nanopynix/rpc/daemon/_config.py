"""Configuration passed from a daemon supervisor to one connection worker."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypeGuard


def _empty_settings() -> dict[str, str]:
    return {}


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


@dataclass(frozen=True)
class DaemonConfig:
    """Immutable Nix configuration for one native daemon listener.

    ``trusted`` is deliberately opt-in. A supervisor must authenticate a peer
    before creating a trusted connection worker for it.
    """

    store_uri: str
    settings: dict[str, str] = field(default_factory=_empty_settings)
    load_config: bool = False
    nix_conf: str | None = None
    trusted: bool = False

    def __post_init__(self) -> None:
        if not self.store_uri:
            raise ValueError("store_uri must not be empty")

    def to_bytes(self) -> bytes:
        """Encode the process-private wire representation."""
        return json.dumps(
            {
                "store_uri": self.store_uri,
                "settings": self.settings,
                "load_config": self.load_config,
                "nix_conf": self.nix_conf,
                "trusted": self.trusted,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> DaemonConfig:
        """Decode and validate configuration received over the inherited pipe."""
        raw: object = json.loads(data)
        if not _is_object_dict(raw):
            raise TypeError("daemon configuration must be a JSON object")
        config: dict[object, object] = raw
        store_uri = config.get("store_uri")
        raw_settings = config.get("settings", {})
        load_config = config.get("load_config", False)
        nix_conf = config.get("nix_conf")
        trusted = config.get("trusted", False)
        if not isinstance(store_uri, str):
            raise TypeError("daemon configuration store_uri must be a string")
        if not _is_object_dict(raw_settings):
            raise TypeError("daemon configuration settings must map strings to strings")
        settings: dict[str, str] = {}
        for name, value in raw_settings.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("daemon configuration settings must map strings to strings")
            settings[name] = value
        if not isinstance(load_config, bool):
            raise TypeError("daemon configuration load_config must be a boolean")
        if nix_conf is not None and not isinstance(nix_conf, str):
            raise TypeError("daemon configuration nix_conf must be a string or null")
        if not isinstance(trusted, bool):
            raise TypeError("daemon configuration trusted must be a boolean")
        return cls(
            store_uri=store_uri,
            settings=settings,
            load_config=load_config,
            nix_conf=nix_conf,
            trusted=trusted,
        )
