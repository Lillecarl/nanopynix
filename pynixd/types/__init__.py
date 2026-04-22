"""
Shared types and enums for Nix daemon operations.
"""

from __future__ import annotations

from .auth import Role as Role
from .build import (
    BuildMode as BuildMode,
    BuildResult as BuildResult,
    BuildResultStatus as BuildResultStatus,
    BuiltOutput as BuiltOutput,
)
from .context import RequestContext as RequestContext
from .derivation import (
    BasicDerivation as BasicDerivation,
    DerivationOutput as DerivationOutput,
    OutputKind as OutputKind,
)
from .logs import OperationLogs as OperationLogs
from .path_info import (
    SubstitutablePathInfo as SubstitutablePathInfo,
    UnkeyedValidPathInfo as UnkeyedValidPathInfo,
    ValidPathInfo as ValidPathInfo,
)
