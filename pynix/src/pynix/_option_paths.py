"""Match a concrete attribute path against the option it instantiates.

**An `attrsOf (submodule ...)` option is in the index under a placeholder.**
`systemd.services.<name>.requires` is one record, and it stands for every
service a configuration declares. A reader does not type the placeholder,
they type the instance they have: `systemd.services.asdf.requires`.

Measured over one NixOS configuration, before this module existed: 3 774 of
24 941 options carry a placeholder, and `systemd.services.asdf.requires` put
the option it names second, in the fuzzy tier, under a bare
`systemd.services`.

**A key that holds a dot is quoted, and that is what keeps this simple.**
Nix writes it `services.nginx.virtualHosts."example.com".root`, so a split
that respects quotes gives one segment for the key and the tail stays in
step. A placeholder then stands for exactly one segment, and the match is a
walk of two lists of equal length. A plain `text.split(".")` is what breaks
that, by turning the quoted key into two.

The match reports what each placeholder bound to, because the binding is the
concrete path: `<name>` bound to `asdf` says the value to read is
`config.systemd.services.asdf.requires`. Issue #266 needs that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

#: What separates one segment of an attribute path from the next.
SEPARATOR = "."

#: What surrounds a segment that holds a character the path syntax would
#: otherwise read, a dot above all.
QUOTE = '"'

#: What opens and closes a placeholder, and how many columns the pair takes.
#: `<>` names nothing, so a placeholder is longer than the pair itself.
OPEN = "<"
CLOSE = ">"
_BRACKETS = len(OPEN) + len(CLOSE)


def split_path(text: str) -> list[str]:
    """Split a dotted attribute path, keeping a quoted segment whole.

    A quote never survives into a segment, because the quote is syntax and
    the key is the text inside it.
    """
    segments: list[str] = []
    current: list[str] = []
    quoted = False
    for character in text:
        if character == QUOTE:
            quoted = not quoted
        elif character == SEPARATOR and not quoted:
            segments.append("".join(current))
            current = []
        else:
            current.append(character)
    segments.append("".join(current))
    return segments


def is_placeholder(segment: str) -> bool:
    """Say whether *segment* stands for any key rather than for one.

    The module system writes `<name>`, and a submodule that names its key
    writes what it likes inside the angle brackets, so the test is the
    brackets and not the word.
    """
    return len(segment) > _BRACKETS and segment.startswith(OPEN) and segment.endswith(CLOSE)


@dataclass(frozen=True)
class Instance:
    """One concrete path, and what its placeholders stand for."""

    #: The segments the reader typed.
    segments: tuple[str, ...]

    #: Each placeholder of the option, in the order it appears, against the
    #: segment it took. `systemd.services.asdf.requires` against
    #: `systemd.services.<name>.requires` gives `(("<name>", "asdf"),)`.
    bound: tuple[tuple[str, str], ...]

    #: Whether the reader typed the whole path, or only the front of it.
    whole: bool

    @property
    def path(self) -> str:
        """The concrete path, quoted where a segment needs it."""
        return join_path(self.segments)


def _quoted(segment: str) -> str:
    """*segment*, in quotes when the path syntax needs them."""
    return f"{QUOTE}{segment}{QUOTE}" if SEPARATOR in segment else segment


def join_path(segments: Sequence[str]) -> str:
    """The inverse of :func:`split_path`, quoting each segment that needs it.

    A caller that drops or replaces a segment has to write the path back, and
    a plain ``".".join`` loses the quotes that made the split correct in the
    first place.
    """
    return SEPARATOR.join(_quoted(segment) for segment in segments)


def bind(option: Sequence[str], query: Sequence[str]) -> Instance | None:
    """Match *query* against the segments of *option*, or answer `None`.

    A placeholder of *option* takes whatever segment faces it. Every other
    segment must be equal.

    *query* may be shorter than *option*, which is the reader part-way
    through typing. The answer then says `whole=False`, so a caller can rank
    it below one that lines up to the end.

    **The last segment of *query* may be the front of the segment it faces.**
    A reader types one character at a time, and every prefix of what they are
    typing has to keep the option they are heading for. Without this,
    `systemd.services.nix.na` matched no option at all and fell to the fuzzy
    tier, one keystroke after `systemd.services.nix` had put the right
    records at the top: measured, the fuzzy answer put
    `systemd.services.<name>.enable` second and `services.nginx.enable`
    fourth. An earlier segment still has to be equal, because a path is
    hierarchical and a reader who typed the dot has finished that segment.
    """
    if not query or len(query) > len(option):
        return None
    last = len(query) - 1
    # An empty segment is the reader who has just typed the dot, and it needs
    # a segment in front of it to mean that. A query of one empty segment is
    # an empty query, which names no path and must match nothing.
    if last == 0 and not query[0]:
        return None
    bound: list[tuple[str, str]] = []
    for index, (want, got) in enumerate(zip(option, query, strict=False)):
        if is_placeholder(want):
            # An empty final segment is the reader who has just typed the
            # dot. It binds nothing yet, and the path is not concrete, so it
            # stays out of `bound`.
            if got:
                bound.append((want, got))
            elif index != last:
                return None
        elif want != got and not (index == last and want.startswith(got)):
            return None
    ends_on_a_whole_segment = bool(query[last]) and (is_placeholder(option[last]) or option[last] == query[last])
    return Instance(
        segments=tuple(query),
        bound=tuple(bound),
        whole=len(query) == len(option) and ends_on_a_whole_segment,
    )
