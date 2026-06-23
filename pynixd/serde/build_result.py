from __future__ import annotations

from ..constants import proto
from .opt_microseconds import OptMicroseconds
from .wire_message import WireField, WireModel


class BuildResult(WireModel):
    """Nix daemon protocol BuildResult.

    Fields present based on protocol version:
    - All versions: status, error_msg
    - >= 1.29: times_built, is_non_deterministic, start_time, stop_time
    - >= 1.37: cpu_user, cpu_system
    - >= 1.28: built_outputs (dict[str, str])
    """

    status: int = 0
    error_msg: str = ""

    # Protocol 1.29 fields
    times_built: int | None = WireField(default=None, min_version=proto(1, 29))
    is_non_deterministic: int | None = WireField(default=None, min_version=proto(1, 29))
    start_time: int | None = WireField(default=None, min_version=proto(1, 29))
    stop_time: int | None = WireField(default=None, min_version=proto(1, 29))

    # Protocol 1.37 fields
    cpu_user: OptMicroseconds = WireField(default_factory=OptMicroseconds, min_version=proto(1, 37))
    cpu_system: OptMicroseconds = WireField(default_factory=OptMicroseconds, min_version=proto(1, 37))

    # Protocol 1.28 fields
    built_outputs: dict[str, str] | None = WireField(default=None, min_version=proto(1, 28))
