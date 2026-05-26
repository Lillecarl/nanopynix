"""
Shared application context for pynixd dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import PynixdSettings
    from .gc import GarbageCollector
    from .local_store_db import LocalStoreDB
    from .path_tracker import PathTracker
    from .scheduler import Scheduler
    from .store.base import Store
    from .types.ids import StoreId


@dataclass
class PynixdContext:
    """Encapsulates shared services and state for dependency injection."""

    settings: PynixdSettings
    local_store: Store
    _stores: dict[StoreId, Store]
    path_tracker: PathTracker
    db: LocalStoreDB | None = None
    scheduler: Scheduler | None = None
    gc: GarbageCollector | None = None

    @property
    def stores(self) -> Mapping[StoreId, Store]:
        """Read-only view of connected remote stores."""
        return self._stores
