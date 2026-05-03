"""
Shared types and enums for Nix daemon operations.
"""

from __future__ import annotations

from .auth import Role as Role
from .build import (
    BuildMode as BuildMode,
)
from .build import (
    BuildResult as BuildResult,
)
from .build import (
    BuildResultStatus as BuildResultStatus,
)
from .build import (
    BuiltOutput as BuiltOutput,
)
from .ca import Realisation as Realisation
from .context import RequestContext as RequestContext
from .derivation import (
    BasicDerivation as BasicDerivation,
)
from .derivation import (
    DerivationOutput as DerivationOutput,
)
from .derivation import (
    OutputKind as OutputKind,
)
from .ids import BuildId as BuildId
from .ids import RequestId as RequestId
from .ids import StoreId as StoreId
from .logs import OperationLogs as OperationLogs
from .path_info import (
    SubstitutablePathInfo as SubstitutablePathInfo,
)
from .path_info import (
    UnkeyedValidPathInfo as UnkeyedValidPathInfo,
)
from .path_info import (
    ValidPathInfo as ValidPathInfo,
)
from .aliases import OutputMap as OutputMap
from .aliases import OutputName as OutputName
from .aliases import StorePathSet as StorePathSet
