"""Shared command-line evaluation target handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from clypi import arg
from nanopynix_helpers import EvaluationTargetError as EvaluationTargetError, select_attr as select_attr

from nanopynix import NixType
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check

if TYPE_CHECKING or BEARTYPING:
    from pathlib import Path

    from nanopynix import AsyncEvalSession, AsyncLockedFlake, AsyncReplSession, AsyncValue


@no_runtime_type_check  # clypi's arg() returns a PartialConfig placeholder at declaration time, not the annotated type -- clypi's own machinery replaces it later; beartype would otherwise flag every call as a type violation
def file_option() -> Path | None:
    """Declare the common ``--file`` option."""
    return arg(None, short="f", help="Evaluate FILE as a Nix expression.")


@no_runtime_type_check  # see file_option
def attr_option() -> str | None:
    """Declare the common ``--attr`` option."""
    return arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")


@no_runtime_type_check  # see file_option
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


async def evaluate_target[ValueT: AsyncValue](
    target: EvaluationTarget,
    session: AsyncEvalSession[ValueT],
    *,
    auto_call_file: bool = False,
) -> ValueT:
    """Evaluate *target* in *session* and apply its attribute selectors."""
    value, locked = await evaluate_target_locked(target, session, auto_call_file=auto_call_file)
    if locked is not None:
        await locked.release()
    return value


async def evaluate_target_locked[ValueT: AsyncValue](
    target: EvaluationTarget,
    session: AsyncEvalSession[ValueT],
    *,
    auto_call_file: bool = False,
) -> tuple[ValueT, AsyncLockedFlake | None]:
    """Evaluate *target*, and also hand back the flake lock when there is one.

    A caller that only wants the value calls :func:`evaluate_target`, which
    releases the lock for it. This form exists for ``pynix develop``, which has
    a second question to ask: which ``nixpkgs`` does the target flake lock?
    ``nix develop`` asks the same question, of the same lock
    (``InstallableFlake::nixpkgsFlakeRef``).

    The lock is ``None`` for a ``--file`` target, which has no flake and no
    lock file.

    The flake branch is ``lock_flake`` and then ``eval``, where it used to be
    ``eval_flake``. Those two do the same work with the same flags --
    ``eval_flake`` is the pair in one call, and it throws the lock away.
    """
    target.validate(required=True)
    locked: AsyncLockedFlake | None = None
    if target.file is not None:
        value = await session.file(str(target.file))
        if auto_call_file:
            value = await value.auto_call()
    else:
        if target.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        ref, _, flake_attr = target.flake.partition("#")
        locked = await session.lock_flake(ref)
        value = cast("ValueT", await locked.eval())
        if flake_attr:
            value = await select_attr(value, flake_attr)

    if target.attr:
        value = await select_attr(value, target.attr)
    return value, locked


async def derivation_path(value: AsyncValue) -> str:
    """Return the ``drvPath`` of *value*, which must be a derivation.

    Raises :class:`EvaluationTargetError` when it is not one, so a caller
    reports it the same way it reports a bad ``--attr``.
    """
    # has_attr() is an attrset question, so ask whether this is an attrset
    # first. It used to answer False for any non-attrset, which made
    # "is this a derivation?" work by accident on a string or a list; it
    # now raises, which is right for an accessor but means the type test
    # has to be explicit.
    if await value.get_type() != NixType.ATTRS or not await value.has_attr("type"):
        raise EvaluationTargetError("value is not a derivation")
    value_type = await value.attr("type").to_python()
    if value_type != "derivation":
        raise EvaluationTargetError(f"value at attribute path is not a derivation (got {value_type!r})")
    path = await value.attr("drvPath").to_python()
    if not isinstance(path, str):
        raise EvaluationTargetError("failed to get derivation path")
    return path


async def load_repl_target[ValueT: AsyncValue](target: EvaluationTarget, repl: AsyncReplSession[ValueT]) -> ValueT:
    """Load *target* into a REPL, preserving Nix's ``:load`` file semantics."""
    target.validate(required=True)
    if target.file is not None:
        value = await repl.load_file(str(target.file))
        if target.attr:
            value = await select_attr(value, target.attr)
        return value
    return await evaluate_target(target, repl)
