"""Shared command-line evaluation target handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from anyio import Path as AsyncPath
from clypi import arg
from nanopynix_helpers import (
    AttrPathSearch as AttrPathSearch,
    EvaluationTargetError as EvaluationTargetError,
    select_attr as select_attr,
    select_flake_attr as select_flake_attr,
)

import nanopynix
from nanopynix import NixType
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check

if TYPE_CHECKING or BEARTYPING:
    from nanopynix import AsyncEvalSession, AsyncLockedFlake, AsyncReplSession, AsyncValue

_FLAKE_PREFIX = "flake:"


@no_runtime_type_check  # clypi's arg() returns a PartialConfig placeholder at declaration time, not the annotated type -- clypi's own machinery replaces it later; beartype would otherwise flag every call as a type violation
def file_option() -> str | None:
    """Declare the common ``--file`` option.

    The value is a string, and not a ``Path``. ``PurePath`` collapses a
    repeated separator, so ``https://example.com/x.tar.gz`` reached the
    evaluator as ``https:/example.com/x.tar.gz`` and failed. A reference is
    also not a path: ``github:NixOS/nixpkgs`` and ``<nixpkgs>`` name a tree
    that no local directory holds.
    """
    return arg(
        None,
        short="f",
        help="Evaluate FILE as a Nix expression. FILE is a path, a lookup path, a URL, or a flake reference, and it may end with '#' and an attribute path.",
    )


@no_runtime_type_check  # see file_option
def attr_option() -> str | None:
    """Declare the common ``--attr`` option."""
    return arg(None, short="A", help="Dot-separated attribute path within the evaluation result.")


@no_runtime_type_check  # see file_option
def flake_option() -> str | None:
    """Declare the common ``--flake`` option."""
    return arg(None, help="Evaluate FLAKE, optionally with a '#'-separated attribute path.")


@dataclass(frozen=True)
class FileReference:
    """A resolved ``--file`` argument."""

    argument: str
    """What the evaluator receives. It is the reference with no fragment."""

    fragment: str | None
    """The attribute path after the first ``#``, or ``None``."""

    local_path: Path | None
    """The local file or directory, when the argument names one that exists."""


async def _existing_local_path(candidate: str) -> Path | None:
    """Return the local path that *candidate* names, or ``None``."""
    if not candidate:
        return None
    try:
        if await AsyncPath(candidate).exists():
            return Path(candidate)
    except OSError:
        # A name that the file system refuses -- too long, or an embedded NUL.
        # Such a name is not a local path, which is the whole question here.
        return None
    return None


async def resolve_file_reference(raw: str) -> FileReference:
    """Resolve a ``--file`` argument into what the evaluator understands.

    The evaluator resolves four shapes itself, in ``lookup_file_arg``: a
    pseudo-URL, the ``flake:`` prefix, a ``<name>`` lookup path, and an
    ordinary path. This function adds two things on top of them, and it
    changes none of the four.

    The first addition is the fragment. ``--flake`` splits on ``#`` already,
    and ``--file`` did not, so an attribute path had to go in ``--attr``.

    The second is a bare flake reference. ``github:NixOS/nixpkgs`` and the
    indirect name ``nixpkgs`` reach the evaluator as ``flake:`` plus the
    reference, which is the branch that fetches the tree. The file inside that
    tree is then evaluated as an ordinary Nix file. This does not read
    ``flake.nix``, and it is not ``--flake``.

    Six rules apply, in this order:

    1. the whole argument names a local path that exists;
    2. the part before the first ``#`` names a local path that exists;
    3. that part starts with ``.``, ``/`` or ``~``, so it is a path that is
       absent;
    4. it is a ``<name>`` lookup path;
    5. Nix fetches it already, which is a pseudo-URL or the ``flake:`` prefix;
    6. anything else becomes ``flake:`` plus the reference.

    Rule 1 and rule 2 are why ``-f nixpkgs`` still reads a directory named
    ``nixpkgs`` in the working directory. Rule 3 is why an absent
    ``./default.nix`` reports a missing file, and not a missing flake. Write
    a relative path with ``./``, because rule 6 reads a bare word as a
    reference.
    """
    if not raw:
        raise EvaluationTargetError("--file needs a value")
    if raw == "-":
        raise EvaluationTargetError(
            "--file - reads an expression from standard input, which pynix does not support yet"
        )

    # A path that exists exactly as written wins, '#' and all. A file name that
    # holds a '#' is rare, and the caller who has one wrote it deliberately.
    whole = await _existing_local_path(raw)
    if whole is not None:
        return FileReference(argument=raw, fragment=None, local_path=whole)

    head, _, tail = raw.partition("#")
    fragment = tail or None
    if not head:
        raise EvaluationTargetError(f"--file {raw!r} names no reference before the '#'")

    local = await _existing_local_path(head)
    if local is not None:
        return FileReference(argument=head, fragment=fragment, local_path=local)

    # A path that the caller wrote as a path stays a path, even when the file
    # is absent. Otherwise a typed `-f ./buidl.nix` would reach the registry,
    # and the caller would read an error about a flake instead of the missing
    # file. `local_path` stays None, because nothing is there to rewrite.
    if head.startswith(("./", "../", "/", "~/")) or head in {".", "..", "~"}:
        return FileReference(argument=head, fragment=fragment, local_path=None)

    # Ask Nix which strings it fetches itself. Repeating the list of schemes
    # here would make this file disagree with the evaluator as soon as Nix adds
    # one, and the disagreement would show as a fetch of 'flake:https://...'.
    handled = nanopynix.is_pseudo_url(head) or head.startswith(_FLAKE_PREFIX) or _is_lookup_path(head)
    argument = head if handled else f"{_FLAKE_PREFIX}{head}"
    return FileReference(argument=argument, fragment=fragment, local_path=None)


def _is_lookup_path(candidate: str) -> bool:
    """Report whether *candidate* is a ``<name>`` lookup path."""
    return len(candidate) > 2 and candidate.startswith("<") and candidate.endswith(">")  # noqa: PLR2004 -- '<' and '>' plus one character, which is the rule in lookup_file_arg


# --- the attribute-path search of each command -------------------------------
#
# `nix` gives every installable command two lists, and a fragment such as
# `#hello` is resolved against them rather than read as one path.
# `SourceExprCommand` in `src/libcmd/installables.cc` holds the base pair, and
# each command overrides it. These functions are that table, and each one names
# the file of the `nix` source that decides it.
#
# **The system comes from this process.** Under the rpc engine Nix lives in the
# worker, and the worker holds the settings that decide the answer, so a worker
# configured with a different `eval-system` would disagree. Issue #114 tracks
# that, and every caller here goes through one function so that the repair has
# one seam.


def _eval_system() -> str:
    """Return the system that names each attribute of the search."""
    return nanopynix.current_system()


def base_attr_search() -> AttrPathSearch:
    """The pair of `SourceExprCommand`, which `build` and `eval` use."""
    system = _eval_system()
    return AttrPathSearch(
        prefixes=(f"packages.{system}.", f"legacyPackages.{system}."),
        defaults=(f"packages.{system}.default", f"defaultPackage.{system}"),
    )


def dev_shell_attr_search() -> AttrPathSearch:
    """The pair of `nix develop`, from `src/nix/develop.cc`."""
    system = _eval_system()
    base = base_attr_search()
    return AttrPathSearch(
        prefixes=(f"devShells.{system}.", *base.prefixes),
        defaults=(f"devShells.{system}.default", f"devShell.{system}", *base.defaults),
    )


def repl_attr_search() -> AttrPathSearch:
    """The pair of `nix repl`, from `src/nix/repl.cc`.

    `CmdRepl` overrides the defaults to one empty path, and it leaves the
    prefixes alone. So `pynix repl --flake <ref>` puts every output of the
    flake into scope, and `--flake <ref>#hello` still finds
    `packages.<system>.hello`.

    An empty path selects the value itself, because Nix's path parser reads
    no component from an empty string.
    """
    return AttrPathSearch(prefixes=base_attr_search().prefixes, defaults=("",))


def app_attr_search() -> AttrPathSearch:
    """The pair of `nix run` and `nix bundle`, from `src/nix/run.cc`.

    No command uses this yet. It is here because `run` is issue #84, and the
    table belongs in one place rather than beside the command that arrives
    last.
    """
    system = _eval_system()
    base = base_attr_search()
    return AttrPathSearch(
        prefixes=(f"apps.{system}.", *base.prefixes),
        defaults=(f"apps.{system}.default", f"defaultApp.{system}", *base.defaults),
    )


def formatter_attr_search() -> AttrPathSearch:
    """The pair of `nix fmt`, from `src/nix/formatter.cc`.

    It has no prefix, so a fragment names the output exactly. Issue #89 adds
    the command.
    """
    return AttrPathSearch(prefixes=(), defaults=(f"formatter.{_eval_system()}",))


@dataclass(frozen=True)
class EvaluationTarget:
    """A file or flake evaluation source with an optional attribute selector."""

    file: str | None
    attr: str | None
    flake: str | None

    @classmethod
    def from_command(cls, command: Any) -> EvaluationTarget:
        """Construct a target from a command declaring the common options."""
        return cls(file=command.file, attr=command.attr, flake=command.flake)

    async def file_reference(self) -> FileReference | None:
        """Resolve ``--file``, or return ``None`` when there is no ``--file``."""
        if self.file is None:
            return None
        return await resolve_file_reference(self.file)

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
    attr_search: AttrPathSearch | None = None,
) -> ValueT:
    """Evaluate *target* in *session* and apply its attribute selectors."""
    value, locked = await evaluate_target_locked(
        target,
        session,
        auto_call_file=auto_call_file,
        attr_search=attr_search,
    )
    if locked is not None:
        await locked.release()
    return value


async def evaluate_target_locked[ValueT: AsyncValue](
    target: EvaluationTarget,
    session: AsyncEvalSession[ValueT],
    *,
    auto_call_file: bool = False,
    attr_search: AttrPathSearch | None = None,
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

    *attr_search* names the candidates that the fragment of ``--flake``
    resolves against, and a command that copies a ``nix`` subcommand passes
    the pair of that subcommand. ``None`` reads the fragment as one exact
    path, which is right for a command that ``nix`` has not got.

    **The search reaches ``--flake`` only.** ``nix`` builds an
    ``InstallableAttrPath`` for a ``--file`` target, and that class applies no
    prefix: `parseInstallables` hands it the raw path.
    """
    target.validate(required=True)
    locked: AsyncLockedFlake | None = None
    if target.file is not None:
        reference = await resolve_file_reference(target.file)
        value = await session.file(reference.argument)
        # Auto-call first, then the fragment. The root of a fetched tree is
        # usually a function, and its attributes only exist after the call.
        # `--attr` has behaved this way since it existed, and the fragment is
        # the same selection written in a different place.
        if auto_call_file:
            value = await value.auto_call()
        if reference.fragment:
            value = await select_attr(value, reference.fragment)
    else:
        if target.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        ref, _, flake_attr = target.flake.partition("#")
        locked = await session.lock_flake(ref)
        value = cast("ValueT", await locked.eval())
        if attr_search is not None:
            value, _found = await select_flake_attr(value, attr_search, flake_attr or None, flake_ref=ref)
        elif flake_attr:
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


async def load_repl_target[ValueT: AsyncValue](
    target: EvaluationTarget,
    repl: AsyncReplSession[ValueT],
    *,
    attr_search: AttrPathSearch | None = None,
) -> ValueT:
    """Load *target* into a REPL, preserving Nix's ``:load`` file semantics."""
    target.validate(required=True)
    if target.file is not None:
        reference = await resolve_file_reference(target.file)
        value = await repl.load_file(reference.argument)
        if reference.fragment:
            value = await select_attr(value, reference.fragment)
        if target.attr:
            value = await select_attr(value, target.attr)
        return value
    return await evaluate_target(target, repl, attr_search=attr_search)
