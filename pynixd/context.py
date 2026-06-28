"""
Shared application context for pynixd dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .types.ids import StoreId

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import PynixdSettings
    from .goals import GoalEngine
    from .local_store_db import LocalStoreDB
    from .scheduler import Scheduler
    from .store.daemon import DaemonStore
    from .store.local_daemon import LocalStore


@dataclass
class PynixdContext:
    """Encapsulates shared services and state for dependency injection.

    All stores (including local) live in ``_stores``. The ``local_store``
    property looks up ``StoreId(\"local\")`` in the dict.
    """

    settings: PynixdSettings
    _stores: dict[StoreId, DaemonStore]
    db: LocalStoreDB | None = None
    scheduler: Scheduler | None = None
    goal_engine: GoalEngine | None = None
    output_locations: dict[str, StoreId] = field(default_factory=dict)

    @property
    def local_store(self) -> LocalStore:
        return self._stores[StoreId("local")]  # type: ignore[return-value]

    @property
    def stores(self) -> Mapping[StoreId, DaemonStore]:
        """Read-only view of all connected stores (including local)."""
        return self._stores
