"""ABC for build goal execution handlers.

Each handler encapsulates one strategy for resolving a build target:
opaque substitution, regular derivation building, or content-addressed
derivation building.  Handlers are stateless — they receive the
:class:`Goal` they operate on via ``execute(goal)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .goal import Goal


class GoalHandler(ABC):
    """Strategy for executing a single build target.

    Subclasses implement :meth:`execute` which reads ``goal.derived_path``,
    resolves dependencies via ``goal.add_child()`` / ``goal.execute_children()``,
    and sets ``goal.result`` when finished.
    """

    @abstractmethod
    async def execute(self, goal: Goal) -> None: ...
