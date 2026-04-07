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


class Op(IntEnum):
    IsValidPath = 1
    QueryReferrers = 6
    AddToStore = 7
    BuildPaths = 9
    EnsurePath = 10
    AddTempRoot = 11
    AddIndirectRoot = 12
    FindRoots = 14
    SetOptions = 19
    CollectGarbage = 20
    QuerySubstitutablePathInfo = 21
    QueryAllValidPaths = 23
    QueryPathInfo = 26
    QueryPathFromHashPart = 29
    QuerySubstitutablePathInfos = 30
    QueryValidPaths = 31
    QuerySubstitutablePaths = 32
    QueryValidDerivers = 33
    OptimiseStore = 34
    VerifyStore = 35
    BuildDerivation = 36
    AddSignatures = 37
    NarFromPath = 38
    AddToStoreNar = 39
    QueryMissing = 40
    QueryDerivationOutputMap = 41
    RegisterDrvOutput = 42
    QueryRealisation = 43
    AddMultipleToStore = 44
    AddBuildLog = 45
    BuildPathsWithResults = 46
    AddPermRoot = 47

    # pynixd extensions (not standard Nix protocol)
    QueryPathInfos = 103
    QueryClosure = 104
    QueryClosureWithInfo = 105
    QueryDerivationOutputsBatch = 106
    SignPathInfo = 107

    # Obsolete ops that we reject
    HasSubstitutes = 3
    QueryPathHash = 4
    QueryReferences = 5
    AddTextToStore = 8
    SyncWithGC = 13
    ExportPath = 16
    QueryDeriver = 18
    QueryDerivationOutputs = 22
    QueryFailedPaths = 24
    ClearFailedPaths = 25
    ImportPaths = 27
    QueryDerivationOutputNames = 28


class OptTrusted(IntEnum):
    Unknown = 0
    Trusted = 1
    NotTrusted = 2
