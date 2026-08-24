"""The full-screen interface of ``pynix osearch``.

``pynix._impl.osearch`` decides which mode to run, and it reaches this module
through the PEP 562 table of ``pynix._impl``. That attribute read is what
imports ``prompt_toolkit`` and the Markdown renderer, so a caller who gave a
query on the command line pays for neither. Measured: this module adds 115
``prompt_toolkit`` modules and 69 Markdown ones.

``pynix._impl._search_tui`` draws the screen, and knows nothing about Nix. This
module is the half that knows what a NixOS option is: how to rank one, and how
to draw one in the detail pane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import to_formatted_text

from nanopynix._typechecking import BEARTYPING
from pynix._impl._search_tui import SearchSource, SearchTui
from pynix._markdown import render_markdown
from pynix._ranking import make_ranker

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from pynix._options import OptionRecord

#: The style classes that the detail pane uses, over the ones that
#: `pynix._impl._search_tui` defines for the screen itself.
STYLE: dict[str, str] = {
    "osearch.name": "bold",
    "osearch.type": "ansicyan",
    "osearch.flag": "ansiyellow",
    "osearch.label": "bold",
    "osearch.path": "ansigreen",
}


def rank(records: Sequence[OptionRecord]) -> Callable[[str], Sequence[OptionRecord]]:
    """Return the function that the interface calls on every keystroke.

    An option matches on its name alone, so the haystack and the name are the
    same text here. `pynix._ranking` holds the algorithm and the measurements
    behind it, and a package search calls the same function with a haystack
    that is wider than a name.
    """
    return make_ranker(records, name=lambda record: record.name)


def detail(record: OptionRecord, width: int) -> StyleAndTextTuples:
    """Draw one option in the pane on the right.

    A description is MyST Markdown, and `render_markdown` is the renderer that
    the REPL uses for the same text. It takes the width of the pane, because
    the pane is one half of a split screen and not the whole terminal.
    """
    fragments: StyleAndTextTuples = [
        ("class:osearch.name", record.name),
        ("", "\n"),
        ("class:osearch.type", record.type),
        ("", "\n"),
    ]
    if record.read_only:
        fragments += [("class:osearch.flag", "read only"), ("", "\n")]
    if record.description:
        fragments.append(("", "\n"))
        fragments += to_formatted_text(render_markdown(record.description, width))
        fragments.append(("", "\n"))
    if record.declarations:
        fragments += [("", "\n"), ("class:osearch.label", "declared in"), ("", "\n")]
        for path in record.declarations:
            fragments += [("class:osearch.path", f"  {path}"), ("", "\n")]
    return fragments


def source(records: Sequence[OptionRecord], subject: str) -> SearchSource[OptionRecord]:
    """Describe the options to the generic interface."""
    return SearchSource(
        items=records,
        rank=rank(records),
        row=lambda record: record.name,
        detail=detail,
        noun="option",
        subject=subject,
        style=STYLE,
    )


async def browse(records: Sequence[OptionRecord], *, subject: str, initial_query: str = "") -> None:
    """Open the full-screen interface over *records*.

    *subject* is what the footer says the search covers, and *initial_query* is
    what the search bar holds when the interface opens.
    """
    await SearchTui(source(records, subject), initial_query=initial_query).run()
