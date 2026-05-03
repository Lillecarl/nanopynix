"""Reusable type aliases for common complex types.

Using ``type`` (Python 3.12+ soft keyword) instead of ``TypeAlias``.
"""

from __future__ import annotations

from ..store_path import StorePath

type OutputName = str
"""Name of a derivation output (e.g. "out", "bin", "lib")."""

type OutputMap = dict[StorePath, dict[OutputName, StorePath | None]]
"""Map of derivation paths to their output maps.

``{drv_path: {output_name: output_path_or_none}}``
An output path is None when it hasn't been realised yet.
"""

type StorePathSet = set[StorePath]
"""Set of store paths — the protocol ``Set<StorePath>``."""
