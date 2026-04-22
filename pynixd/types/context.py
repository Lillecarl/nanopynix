"""Execution context for daemon operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..proxy import DaemonProxy
    from .auth import Role


@dataclass(frozen=True)
class RequestContext:
    """Context passed to operation handlers."""

    proxy: DaemonProxy
    role: Role
    version: int
    username: str
