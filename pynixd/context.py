"""
Shared application context for pynixd dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import PynixdSettings
    from .local_store_db import LocalStoreDB
    from .path_tracker import PathTracker
    from .scheduler import Scheduler
    from .store.base import Store


@dataclass
class PynixdContext:
    """Encapsulates shared services and state for dependency injection."""

    settings: PynixdSettings
    local_store: Store
    stores: dict[str, Store]
    path_tracker: PathTracker
    db: LocalStoreDB | None = None
    scheduler: Scheduler | None = None
