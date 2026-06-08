"""
Shared application context for pynixd dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .goals.manager import GoalManager
from .types.ids import StoreId

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import PynixdSettings
    from .local_store_db import LocalStoreDB
    from .path_tracker import PathTracker
    from .scheduler import Scheduler
    from .store.base import Store
    from .substitution import SubstitutionManager


@dataclass
class PynixdContext:
    """Encapsulates shared services and state for dependency injection.

    All stores (including local) live in ``_stores``. The ``local_store``
    property looks up ``StoreId(\"local\")`` in the dict.
    """

    settings: PynixdSettings
    _stores: dict[StoreId, Store]
    path_tracker: PathTracker
    goal_manager: GoalManager = field(default_factory=GoalManager)
    substitution_manager: SubstitutionManager | None = None
    db: LocalStoreDB | None = None
    scheduler: Scheduler | None = None

    @property
    def local_store(self) -> Store:
        return self._stores[StoreId("local")]

    @property
    def stores(self) -> Mapping[StoreId, Store]:
        """Read-only view of all connected stores (including local)."""
        return self._stores
