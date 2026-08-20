"""What a Nix CLI offers when a caller presses Tab on an attribute path.

`nanopynix_helpers.eval_target` selects the attribute path that a caller
finished typing. This module lists the paths they could still mean, which is
the same question asked one keypress earlier.

**Nix answers it in two places, by two different rules, and both are here.**
`SourceExprCommand::completeInstallable` of `src/libcmd/installables.cc` has
one branch for ``--file`` and one for a flake, and they do not agree:

- the ``--file`` branch splits on the **last literal dot** and hands the left
  half back to the caller unchanged. So ``packages."a.b".he`` keeps its
  quotation marks in every candidate.
- the flake branch parses the whole path with ``parseAttrPath``, resolves it
  against several roots, and rebuilds each candidate from the symbols it
  found. So the quotation marks are gone, and Nix carries a ``FIXME: handle
  names with dots`` on the line that does it.

Copying one rule to both places would be wrong in one of them, so
:func:`complete_file_attr_path` and :func:`complete_flake_fragment` are two
functions.

**Neither one opens a store, and neither one has a budget.** Both take a value
that the caller already evaluated, and both answer a list. A completion runs
while a person holds a key down, so the program that calls these needs to give
up: ``pynix._attr_completion`` holds that part, and it is a policy of a program
and not of this library.

**No value is forced.** Listing the names of an attribute set forces the set
and not what is in it, and neither function asks for anything else. A tree that
holds a derivation which throws still completes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixTypeError
from nanopynix_helpers.eval_target import AttrPathNotFoundError, AttrPathSearch, parse_attr_path, select_attr_path

# `or BEARTYPING`, because beartype resolves an annotation at call time and a
# name that only a type checker imported is not there to resolve. Measured:
# every test of `test_attr_completion.py` failed with
# `BeartypeCallHintForwardRefException: Forward reference "AsyncValue"
# unimportable`. `eval_target.py` beside this file carries the same guard.
if TYPE_CHECKING or BEARTYPING:
    from nanopynix import AsyncValue


async def _attr_names(value: AsyncValue) -> tuple[str, ...]:
    """The names of *value*, or none when *value* is not an attribute set.

    Nix guards the same call with ``if (v2.type() == nAttrs)`` and offers
    nothing otherwise, because a caller who typed a path to a string has
    nothing left to complete.
    """
    try:
        return tuple(await value.attr_names())
    except NixTypeError:
        return ()


async def complete_file_attr_path(root: AsyncValue, prefix: str) -> list[str]:
    """The attribute paths of *root* that a caller typing *prefix* could mean.

    This is the ``--file`` branch of `SourceExprCommand::completeInstallable`.

    *root* is the value of the file, already auto-called. The caller does the
    call, because a caller that resolved the file also decided how.

    **A candidate is the whole path, and not the last component.** ``nixos.co``
    gives ``nixos.config``, because a shell replaces the word under the cursor
    and the word is the whole dotted path.

    **The left half comes back exactly as the caller typed it.** Nix splits on
    the last literal dot and concatenates that text again, so a quoted
    component survives the round trip.
    """
    head, separator, tail = prefix.rpartition(".")
    try:
        value = await select_attr_path(root, parse_attr_path(head)) if separator else root
    except AttrPathNotFoundError:
        return []
    lead = f"{head}." if separator else ""
    # Sorted, and each name once, because Nix collects every candidate of both
    # branches into the `std::set<Completion>` of `struct Completions`.
    return sorted({f"{lead}{name}" for name in await _attr_names(value) if name.startswith(tail)})


def _split_fragment(text: str) -> tuple[tuple[str, ...], str]:
    """The resolved components of *text*, and the component being typed.

    Two lines of `completeFlakeRefWithFragment`::

        if (!attrPath.empty() && !hasSuffix(attrPathS, "."))
            lastAttr = ...; attrPath.pop_back();

    A trailing dot means the caller finished the component before it, so every
    name of that set matches. ``"a.b"`` resolves ``a`` and matches ``b``.
    """
    parts = parse_attr_path(text)
    if parts and not text.endswith("."):
        return parts[:-1], parts[-1]
    return parts, ""


async def complete_flake_fragment(outputs: AsyncValue, search: AttrPathSearch, fragment: str) -> list[str]:
    """The fragments of a flake that a caller typing *fragment* could mean.

    This is `completeFlakeRefWithFragment` of `src/libcmd/installables.cc`.
    *outputs* is the evaluated output set of the locked flake.

    **A fragment is resolved against several roots, and the answer is their
    union.** ``#hello`` means ``packages.<system>.hello`` first, then
    ``legacyPackages.<system>.hello``, then ``hello``, so the completion lists
    all three sets. Each candidate comes back with its prefix removed, which is
    the name the caller would type. A name that two roots hold appears once.

    **A leading dot clears the prefixes.** ``#.hello`` is how a caller reaches
    a top-level output that a prefix would otherwise hide, and every candidate
    keeps the dot in front of it.

    **An empty fragment offers itself once, when a default resolves.**
    ``nix build F#`` offers ``F#`` because ``packages.<system>.default`` is
    there and the caller may stop typing. That is why this needs the defaults
    of *search* and not only its prefixes.
    """
    root_mark = ""
    if fragment.startswith("."):
        fragment = fragment[1:]
        root_mark = "."
    # The prefixes of the command, and then the top of the flake. Nix pushes
    # the empty prefix on after it decides whether to clear the rest.
    prefixes = ("",) if root_mark else (*search.prefixes, "")

    found: list[str] = []
    for prefix in prefixes:
        stem, tail = _split_fragment(f"{prefix}{fragment}")
        depth = len(parse_attr_path(prefix))
        try:
            value = await select_attr_path(outputs, stem)
        except AttrPathNotFoundError:
            # A root that does not hold the path is the next root, which is
            # what `if (!attr) continue;` says.
            continue
        # Nix strips the prefix off the front of the path it found, so
        # `packages.<system>.hello` comes back as `hello`.
        head = ".".join(stem[depth:])
        head = f"{head}." if head else ""
        found.extend(f"{root_mark}{head}{name}" for name in await _attr_names(value) if name.startswith(tail))

    if not fragment:
        for default in search.defaults:
            try:
                await select_attr_path(outputs, parse_attr_path(default))
            except AttrPathNotFoundError:
                continue
            found.append(root_mark)
            break

    # Nix collects its candidates in a `std::set`, so each name appears once
    # and the answer is sorted. Two prefixes can hold one name.
    return sorted(set(found))
