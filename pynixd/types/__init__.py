"""
Shared types and enums for Nix daemon operations.
"""

from __future__ import annotations

from .aliases import ContentAddress as ContentAddress
from .aliases import NARHash as NARHash
from .aliases import OutputMap as OutputMap
from .aliases import OutputName as OutputName
from .aliases import StorePathSet as StorePathSet
from .auth import Role as Role
from .ca import Realisation as Realisation
from .context import RequestContext as RequestContext
from .ids import BuildId as BuildId
from .ids import RequestId as RequestId
from .ids import StoreId as StoreId
from .protocol import (
    ActivityType as ActivityType,
)
from .protocol import (
    FieldType as FieldType,
)
from .protocol import (
    FileIngestionMethod as FileIngestionMethod,
)
from .protocol import (
    GCAction as GCAction,
)
from .protocol import (
    OptTrusted as OptTrusted,
)
from .protocol import (
    PynixdGCAction as PynixdGCAction,
)
from .protocol import (
    ResultType as ResultType,
)
from .protocol import (
    Verbosity as Verbosity,
)
