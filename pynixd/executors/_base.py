"""Executor base class and registry for local operation fast-paths."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ..operations.base import OpResponse
    from ..store.base import Store

EXECUTOR_REGISTRY: dict[int, type[Executor]] = {}


class Executor(ABC):
    """Base class for local operation executors.

    An executor provides a fast-path implementation for an operation,
    avoiding daemon round-trips.  Subclasses self-register via
    ``__init_subclass__``.

    ``execute()`` returns a response on success, or ``None`` to signal
    "I can't handle this — fall through to the daemon."
    """

    op: ClassVar[int]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "op" in cls.__dict__:
            EXECUTOR_REGISTRY[cls.op] = cls

    @abstractmethod
    async def execute(
        self,
        request: Any,
        store: Store,
        client: Any = None,
        suppress_last: bool = False,
    ) -> OpResponse | None:
        """Try to execute this operation locally.

        Returns a response on success, or None to fall through to daemon.
        """
        ...
