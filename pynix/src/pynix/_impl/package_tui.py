"""The full-screen interface over packages.

``pynix._impl._search_tui`` draws the screen and knows nothing about Nix. This
module is the half that knows what a package is: how to name one in the list,
and how to draw one in the detail pane. ``pynix._impl.options_tui`` is the same
half for a NixOS option, and the two share the interface and the ranking.

It imports ``prompt_toolkit``, so it lives under ``pynix._impl`` and no
subcommand module may import it. ``pynix._package_search`` holds the join and
the ranking, imports none of that, and is what a caller that prints a list
reads instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from pynix._impl._search_tui import SearchSource
from pynix._package_search import rank

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from pynix._package_search import SearchablePackage

#: The style classes that the detail pane uses, over the ones that
#: `pynix._impl._search_tui` defines for the screen itself.
STYLE: dict[str, str] = {
    "package.name": "bold",
    "package.version": "ansicyan",
    "package.label": "bold",
    "package.command": "ansigreen",
    "package.binary": "ansiblue",
    "package.flag": "ansiyellow",
}


def row(package: SearchablePackage) -> str:
    """Name one package in the list on the left.

    The version comes along, because two packages that differ only by version
    are otherwise the same row twice.
    """
    if package.record.version:
        return f"{package.record.attr}  {package.record.version}"
    return package.record.attr


def detail(package: SearchablePackage, width: int) -> StyleAndTextTuples:
    """Draw one package in the pane on the right.

    A `meta.description` is one line of plain text, and not the MyST that a
    NixOS option carries, so this does not run the Markdown renderer over it.
    """
    record = package.record
    fragments: StyleAndTextTuples = [("class:package.name", record.attr), ("", "\n")]
    if record.version:
        fragments += [("class:package.version", f"{record.pname} {record.version}"), ("", "\n")]
    for flag, present in (("broken", record.broken), ("unfree", record.unfree)):
        if present:
            fragments += [("class:package.flag", flag), ("", "\n")]
    if record.description:
        fragments += [("", "\n"), ("", _wrapped(record.description, width)), ("", "\n")]
    if package.command:
        fragments += [
            ("", "\n"),
            ("class:package.label", "run"),
            ("", "\n  "),
            ("class:package.command", package.command),
            ("", "\n"),
        ]
    if package.binaries:
        fragments += [("", "\n"), ("class:package.label", "gives"), ("", "\n")]
        fragments += [("", "  "), ("class:package.binary", "  ".join(package.binaries)), ("", "\n")]
    return fragments


def _wrapped(text: str, width: int) -> str:
    """Break *text* into lines of at most *width* columns.

    The detail window wraps as well, and this keeps a word whole where the
    window would break it in the middle.
    """
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}" if line else word
        if len(candidate) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return "\n".join(lines)


def source(packages: Sequence[SearchablePackage], subject: str) -> SearchSource[SearchablePackage]:
    """Describe the packages to the generic interface.

    *subject* says where the answers came from, and the footer prints it. Pass
    `ProgramIndex.origin` in it, because a package search reads two sources
    that disagree by design: the walk describes the nixpkgs the caller pinned,
    and the binaries describe one channel release.
    """
    return SearchSource(
        items=packages,
        rank=rank(packages),
        row=row,
        detail=detail,
        noun="package",
        subject=subject,
        style=STYLE,
    )
