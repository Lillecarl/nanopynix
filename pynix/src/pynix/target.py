"""Shared command-line evaluation target handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clypi import arg

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix._session import EvalSession, ReplSession, ValueProxy


class EvaluationTargetError(RuntimeError):
    """An evaluation target or attribute selection is invalid."""


def file_option() -> Path | None:
    """Declare the common ``--file`` option."""
    return arg(None, short="f", help="Evaluate FILE as a Nix expression.")


def attr_option() -> str | None:
    """Declare the common ``--attr`` option."""
    return arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")


def flake_option() -> str | None:
    """Declare the common ``--flake`` option."""
    return arg(None, help="Evaluate FLAKE, optionally with a '#'-separated attribute path.")


@dataclass(frozen=True)
class EvaluationTarget:
    """A file or flake evaluation source with an optional attribute selector."""

    file: Path | None
    attr: str | None
    flake: str | None

    @classmethod
    def from_command(cls, command: Any) -> EvaluationTarget:
        """Construct a target from a command declaring the common options."""
        return cls(file=command.file, attr=command.attr, flake=command.flake)

    def validate(self, *, required: bool = False) -> None:
        """Validate mutually exclusive sources and attribute selection."""
        if self.file is not None and self.flake is not None:
            raise EvaluationTargetError("--file and --flake are mutually exclusive")
        if required and self.file is None and self.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        if self.attr is not None and self.file is None and self.flake is None:
            raise EvaluationTargetError("--attr requires --file or --flake")


async def evaluate_target(
    target: EvaluationTarget,
    session: EvalSession,
    *,
    auto_call_file: bool = False,
) -> ValueProxy:
    """Evaluate *target* in *session* and apply its attribute selectors."""
    target.validate(required=True)
    if target.file is not None:
        value = await session.file(str(target.file))
        if auto_call_file:
            value = await value.auto_call()
    else:
        if target.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        ref, _, flake_attr = target.flake.partition("#")
        value = await session.eval_flake(ref)
        if flake_attr:
            value = await select_attr(value, flake_attr)

    if target.attr:
        value = await select_attr(value, target.attr)
    return value


async def load_repl_target(target: EvaluationTarget, repl: ReplSession) -> ValueProxy:
    """Load *target* into a REPL, preserving Nix's ``:load`` file semantics."""
    target.validate(required=True)
    if target.file is not None:
        value = await repl.load_file(str(target.file))
        if target.attr:
            value = await select_attr(value, target.attr)
        return value
    return await evaluate_target(target, repl)


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
