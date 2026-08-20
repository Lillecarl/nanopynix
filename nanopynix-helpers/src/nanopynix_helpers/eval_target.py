"""Shared attribute selection for nanopynix evaluation-target CLIs.

Two things live here. The first is the selection of one attribute path, which
splits the path the way Nix splits it and reports a missing name with the
names that are there. The second is the *search* that the `nix` CLI performs
over a flake: a fragment such as `#hello` is not one path, it is a list of
candidates that the command decides, and the first candidate that resolves is
the answer.

`AttrPathSearch` holds the two lists that make that decision. They are
`SourceExprCommand::getDefaultFlakeAttrPathPrefixes` and
`getDefaultFlakeAttrPaths` of the `nix` CLI, and each command overrides them:
`nix develop` puts `devShells` in front, `nix run` puts `apps` in front. The
rule that turns them into candidates is `InstallableFlake::getActualAttrPaths`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixTypeError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from nanopynix import AsyncValue


class EvaluationTargetError(RuntimeError):
    """An evaluation target or attribute selection is invalid."""


_MAX_SUGGESTED_ATTRS = 10


def parse_attr_path(attrpath: str) -> tuple[str, ...]:
    """Split *attrpath* into components, the way Nix splits it.

    This is `parseAttrPath` of `src/libexpr/attr-path.cc`. A component may
    carry quotation marks, so ``packages."x86_64-linux".hello`` is three
    components and not four. That form is the only way to name an attribute
    that holds a dot.

    An empty string gives no components, and selecting no component returns
    the value itself. That is what the ``#.`` form of a fragment asks for.
    """
    parts: list[str] = []
    current = ""
    index = 0
    length = len(attrpath)
    while index < length:
        char = attrpath[index]
        if char == ".":
            parts.append(current)
            current = ""
        elif char == '"':
            index += 1
            while True:
                if index == length:
                    raise EvaluationTargetError(f"missing closing quote in selection path {attrpath!r}")
                if attrpath[index] == '"':
                    break
                current += attrpath[index]
                index += 1
        else:
            current += char
        index += 1
    # A trailing separator adds nothing, because Nix appends the last
    # component only when it holds a character. `a.` is one component.
    if current:
        parts.append(current)
    return tuple(parts)


class AttrPathNotFoundError(EvaluationTargetError):
    """One attribute path did not resolve.

    *depth* is the number of components that did resolve, so a caller that
    tries several candidates can report the one that went furthest.
    """

    def __init__(self, message: str, *, depth: int, available: Sequence[str]) -> None:
        super().__init__(message)
        self.depth = depth
        self.available = tuple(available)


def _describe(names: Sequence[str]) -> str:
    listed = ", ".join(names[:_MAX_SUGGESTED_ATTRS])
    suffix = "" if len(names) <= _MAX_SUGGESTED_ATTRS else f", ... ({len(names)} total)"
    return f"{listed}{suffix}"


async def select_attr_path[ValueT: AsyncValue](value: ValueT, parts: Sequence[str]) -> ValueT:
    """Select each component of *parts* in turn.

    Raises :class:`AttrPathNotFoundError` when a component is absent, and also when
    a component asks for an attribute of a value that is not an attribute set.
    Nix treats those two the same way: `InstallableFlake::getCursors` catches
    the type error and moves to the next candidate.
    """
    for depth, part in enumerate(parts):
        if not part:
            raise AttrPathNotFoundError("attribute path contains an empty component", depth=depth, available=())
        try:
            present = await value.has_attr(part)
        except NixTypeError as exc:
            raise AttrPathNotFoundError(str(exc), depth=depth, available=()) from exc
        if not present:
            names = await value.attr_names()
            raise AttrPathNotFoundError(
                f"attribute {part!r} not found; available attributes: {_describe(names)}",
                depth=depth,
                available=names,
            )
        value = value.attr(part)
    return value


async def select_attr[ValueT: AsyncValue](value: ValueT, attrpath: str) -> ValueT:
    """Select one attribute path, with useful missing-attribute errors."""
    return await select_attr_path(value, parse_attr_path(attrpath))


async def flake_outputs[ValueT: AsyncValue](flake: ValueT) -> ValueT:
    """The ``outputs`` of an evaluated flake, which is what `nix` resolves against.

    `callFlake` returns the outputs merged with the metadata of the flake, so
    the value it hands back also holds ``_type``, ``inputs``, ``lastModified``,
    ``lastModifiedDate``, ``narHash``, ``outPath``, ``outputs`` and
    ``sourceInfo``. Every command of `nix` selects ``outputs`` out of it before
    it resolves anything, in `openEvalCache` of `src/libflake/flake.cc`::

        auto aOutputs = vFlake->attrs()->get(state.symbols.create("outputs"));
        assert(aOutputs);
        return aOutputs->value;

    A caller that skips this step accepts ``#outPath`` where `nix` reports that
    the flake does not provide it, and shows the metadata where `nix` shows the
    outputs alone. Issue #228 measured both.

    The step is here and not in the binding. `nanopynix.rpc` returns what
    `callFlake` returns, which is the honest binding and the only way to reach
    the metadata at all.
    """
    return await select_attr(flake, "outputs")


def show_attr_paths(paths: Sequence[str]) -> str:
    """Quote and join *paths* the way Nix reports a failed search.

    This is `showAttrPaths` of `src/libcmd/installable-flake.cc`: ``'a'``,
    ``'a' or 'b'``, ``'a', 'b' or 'c'``.
    """
    quoted = [f"'{path}'" for path in paths]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


@dataclass(frozen=True)
class AttrPathSearch:
    """The attribute paths that one command tries, in the order it tries them.

    *prefixes* apply when the caller gave a fragment, and *defaults* apply when
    the caller gave none. A command that wants no search at all leaves both
    empty, and then a fragment selects exactly what it says.
    """

    prefixes: tuple[str, ...] = ()
    defaults: tuple[str, ...] = ()

    def candidates(self, fragment: str | None) -> tuple[str, ...]:
        """Return every path to try for *fragment*, best first.

        Three rules, and they are `InstallableFlake::getActualAttrPaths`:

        - no fragment gives the defaults, and no prefix applies to them;
        - a fragment that starts with ``.`` loses that character, and it is
          then the only candidate. This is how a caller reaches an output that
          a prefix would otherwise hide;
        - any other fragment is tried under each prefix first, and bare last.
        """
        if not fragment:
            return self.defaults
        if fragment.startswith("."):
            return (fragment[1:],)
        return (*(f"{prefix}{fragment}" for prefix in self.prefixes), fragment)


async def select_flake_attr[ValueT: AsyncValue](
    outputs: ValueT,
    search: AttrPathSearch,
    fragment: str | None,
    *,
    flake_ref: str,
) -> tuple[ValueT, str]:
    """Return the first candidate of *search* that resolves, and its path.

    Raises :class:`EvaluationTargetError` naming every candidate when none
    resolves, which is the message of `InstallableFlake::getCursors`. The
    error also carries the names available at the point where the candidate
    that went furthest stopped, because a list of paths alone does not say
    what the flake *does* provide.
    """
    candidates = search.candidates(fragment)
    if not candidates:
        return outputs, ""
    furthest: AttrPathNotFoundError | None = None
    for candidate in candidates:
        try:
            return await select_attr_path(outputs, parse_attr_path(candidate)), candidate
        except AttrPathNotFoundError as exc:
            if furthest is None or exc.depth > furthest.depth:
                furthest = exc
    message = f"flake {flake_ref!r} does not provide attribute {show_attr_paths(candidates)}"
    if furthest is not None and furthest.available:
        message = f"{message}; available attributes: {_describe(furthest.available)}"
    raise EvaluationTargetError(message)
