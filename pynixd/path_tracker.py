"""Path tracking and synchronization for the Server."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Set as AbstractSet

    from .local_store_db import LocalStoreDB
    from .store_path import StorePath


class PathTrackerInstance:
    """A per-store view of known paths, managed by a central PathTracker.

    If no parent PathTracker is provided, this operates entirely in-memory.
    """

    def __init__(
        self,
        store_id: str,
        parent: PathTracker | None = None,
        initial_paths: set[StorePath] | None = None,
        is_local: bool = False,
    ) -> None:
        self.store_id = store_id
        self.parent = parent
        self._known_paths: set[StorePath] = initial_paths or set()
        self.is_local = is_local

    @property
    def known_paths(self) -> AbstractSet[StorePath]:
        """Read-only view of paths tracked by this instance."""
        return self._known_paths

    def has_path(self, path: StorePath) -> bool:
        return path in self._known_paths

    def has_all_paths(self, paths: set[StorePath]) -> bool:
        return paths.issubset(self._known_paths)

    def count_common_paths(self, paths: set[StorePath]) -> int:
        return len(paths & self._known_paths)

    def add_known_path(self, path: StorePath, *, update_regtime: bool = True) -> None:
        self._known_paths.add(path)
        if self.parent is not None:
            self.parent.notify_path_added(
                self.store_id,
                path,
                update_regtime=update_regtime,
                is_local=self.is_local,
            )

    def add_known_paths(
        self,
        paths: Iterable[StorePath],
        *,
        update_regtime: bool = True,
    ) -> None:
        path_set = set(paths)
        if not path_set:
            return
        self._known_paths.update(path_set)
        if self.parent is not None:
            self.parent.notify_paths_added(
                self.store_id,
                path_set,
                update_regtime=update_regtime,
                is_local=self.is_local,
            )

    def remove_known_paths(
        self,
        paths: Iterable[StorePath],
        *,
        update_regtime: bool = False,
    ) -> None:
        """Remove paths from the known set and notify parent."""
        to_remove = set(paths)
        removed = self._known_paths & to_remove
        if not removed:
            return

        self._known_paths -= removed

        if self.parent is not None:
            self.parent.notify_paths_replaced(
                self.store_id,
                added=set(),
                removed=removed,
                update_regtime=update_regtime,
                is_local=self.is_local,
            )

    def set_known_paths(
        self,
        paths: Iterable[StorePath],
        *,
        update_regtime: bool = True,
    ) -> None:
        """Replace the current known paths with a new set."""
        new_paths = set(paths)
        removed = self._known_paths - new_paths
        added = new_paths - self._known_paths

        self._known_paths = new_paths

        if self.parent is not None:
            self.parent.notify_paths_replaced(
                self.store_id,
                added,
                removed,
                update_regtime=update_regtime,
                is_local=self.is_local,
            )


class PathTracker:
    """Centralized path tracking coordinator owned by the Server.

    Synchronizes in-memory path states of remote stores with the central
    LocalStoreDB to persist caching and update GC registration times.
    """

    def __init__(self, db: LocalStoreDB | None) -> None:
        self.db = db

    def get_instance(
        self,
        store_id: str,
        is_local: bool = False,
        initial_paths: set[StorePath] | None = None,
    ) -> PathTrackerInstance:
        """Create a tracked instance for a store."""
        return PathTrackerInstance(
            store_id=store_id,
            parent=self,
            initial_paths=initial_paths,
            is_local=is_local,
        )

    def notify_path_added(
        self,
        store_id: str,
        path: StorePath,
        update_regtime: bool,
        is_local: bool,
    ) -> None:
        if self.db is None:
            return
        if update_regtime:
            self.db.mark_path(path)
        if not is_local:
            self.db.mark_known_paths(store_id, {path})

    def notify_paths_added(
        self,
        store_id: str,
        paths: set[StorePath],
        update_regtime: bool,
        is_local: bool,
    ) -> None:
        if self.db is None:
            return
        if update_regtime:
            self.db.mark_paths(paths)
        if not is_local:
            self.db.mark_known_paths(store_id, paths)

    def notify_paths_replaced(
        self,
        store_id: str,
        added: set[StorePath],
        removed: set[StorePath],
        update_regtime: bool,
        is_local: bool,
    ) -> None:
        if self.db is None:
            return
        if update_regtime and added:
            self.db.mark_paths(added)
        if not is_local:
            if added:
                self.db.mark_known_paths(store_id, added)
            if removed:
                self.db.mark_removed_known_paths(store_id, removed)
