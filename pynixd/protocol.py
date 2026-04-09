"""
Nix daemon protocol types and operation codes.
Protocol versions 1.32+ are supported (negotiated per connection).
"""

from enum import IntEnum
from functools import cache

import structlog


@cache
def op_log(op_name: str):
    """Per-op logger: pynixd.op.{OpName}.

    Allows silencing noisy ops individually, e.g.:
        logging.getLogger("pynixd.op.QueryPathInfo").setLevel(logging.WARNING)
    """
    return structlog.get_logger(f"pynixd.op.{op_name}")


EXTENSION_FEATURES: set[str] = {
    "QueryPathInfos",
    "QueryClosure",
    "QueryClosureWithInfo",
    "QueryDerivationOutputsBatch",
    "SignPathInfo",
}


def get_extension_features() -> set[str]:
    """Return all pynixd extension operation names."""
    return EXTENSION_FEATURES


class OptTrusted(IntEnum):
    Unknown = 0
    Trusted = 1
    NotTrusted = 2
