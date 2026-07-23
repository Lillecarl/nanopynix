"""Shared dot-path attribute selection for nanopynix evaluation-target CLIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanopynix.rpc.client import ValueProxy


class EvaluationTargetError(RuntimeError):
    """An evaluation target or attribute selection is invalid."""


async def select_attr(value: ValueProxy, attrpath: str) -> ValueProxy:
    """Select a dot-separated attribute path with useful missing-attribute errors."""
    for part in attrpath.split("."):
        if not part:
            raise EvaluationTargetError("attribute path contains an empty component")
        if not await value.has_attr(part):
            names = await value.attr_names()
            available = ", ".join(names[:10])
            suffix = "" if len(names) <= 10 else f", ... ({len(names)} total)"
            raise EvaluationTargetError(f"attribute {part!r} not found; available attributes: {available}{suffix}")
        value = value.attr(part)
    return value
