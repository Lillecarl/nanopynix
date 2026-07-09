"""Public type aliases for eval proxy values."""

from __future__ import annotations

from nanopynix._session import ValueAttrs, ValueList, ValueProxy
from nanopynix.models import JsonScalar, JsonValue

type NixArg = ValueProxy | JsonScalar | list[NixArg] | dict[str, NixArg]
type NixValue = ValueProxy | ValueAttrs | ValueList | JsonValue
type NixDeepValue = ValueProxy | JsonScalar | list[NixDeepValue] | dict[str, NixDeepValue]

__all__ = [
    "NixArg",
    "NixDeepValue",
    "NixValue",
]
