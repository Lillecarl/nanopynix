"""Protocol-level enum types for the Nix daemon wire protocol."""

from __future__ import annotations

from enum import IntEnum


class Verbosity(IntEnum):
    """Log verbosity levels — uint64 on the wire."""

    ERROR = 0
    WARN = 1
    NOTICE = 2
    INFO = 3
    TALKATIVE = 4
    CHATTY = 5
    DEBUG = 6
    VOMIT = 7


class GCAction(IntEnum):
    """Actions for collect-garbage operation — uint64 on the wire."""

    RETURN_LIVE = 0
    RETURN_DEAD = 1
    DELETE_DEAD = 2
    DELETE_SPECIFIC = 3
