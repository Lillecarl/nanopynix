"""The full-screen interface of ``pynix search``.

``pynix._impl.search`` decides which mode to run, and it reaches this module
through the PEP 562 table of ``pynix._impl``. That attribute read is what
imports ``prompt_toolkit`` and the Markdown renderer, so a caller who gave a
query on the command line pays for neither. Measured: this module adds 115
``prompt_toolkit`` modules and 69 Markdown ones.

``pynix._impl._search_tui`` draws the screen, and knows nothing about Nix. This
module is the half that knows how to draw a NixOS option in the detail pane.

**The ranking is not here.** ``pynix._option_search`` holds it, so that a
caller who prints a list, and a caller who merges options with packages, reach
it without importing the screen. Issue #257 moved it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import to_formatted_text

from nanopynix._typechecking import BEARTYPING
from pynix._impl._search_tui import SearchSource, SearchTui
from pynix._markdown import render_markdown
from pynix._option_search import rank

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from pynix._option_values import OptionValues, Value
    from pynix._options import OptionRecord

#: The style classes that the detail pane uses, over the ones that
#: `pynix._impl._search_tui` defines for the screen itself.
#:
#: **The namespace is `option`, and never `search`.** prompt_toolkit's own
#: default style defines `search` as `bg:ansibrightyellow ansiblack`, and it
#: matches a class by its dotted prefix. So a class named `search.name` takes
#: that background, and the whole pane draws black on bright yellow. The
#: generic layer met this first and answers it the same way, with `search-tui`.
#: `pynix/tests/test_search_tui.py` holds the check that keeps every namespace
#: of this program clear of one that prompt_toolkit already owns.
STYLE: dict[str, str] = {
    "option.name": "bold",
    "option.type": "ansicyan",
    "option.flag": "ansiyellow",
    "option.label": "bold",
    "option.path": "ansigreen",
    "option.value": "ansiwhite",
    "option.pending": "italic",
    "option.error": "ansired",
}

#: What each line of a rendered value starts with, so that a value of several
#: lines reads as one block under its label.
_INDENT = "  "

#: What the pane says while the evaluator is still working. The first option a
#: reader selects pays for the whole options tree, so this line is on the
#: screen for about 5 s and then never again.
_PENDING = "resolving the default and the example..."


def detail(record: OptionRecord, width: int, values: OptionValues | None = None) -> StyleAndTextTuples:
    """Draw one option in the detail pane under the list.

    A description is MyST Markdown, and `render_markdown` is the renderer that
    the REPL uses for the same text. It takes the width of the pane, which is
    the full width of the terminal since issue #261 stacked the two panes.

    *values* forces the `default` and the `example`, which the index does not
    hold. It is `None` for a search that has no evaluator to open.
    """
    fragments: StyleAndTextTuples = [
        ("class:option.name", record.name),
        ("", "\n"),
        ("class:option.type", record.type),
        ("", "\n"),
    ]
    if record.read_only:
        fragments += [("class:option.flag", "read only"), ("", "\n")]
    if record.description:
        fragments.append(("", "\n"))
        fragments += to_formatted_text(render_markdown(record.description, width))
        fragments.append(("", "\n"))
    fragments += _values(record, width, values)
    if record.declarations:
        fragments += [("", "\n"), ("class:option.label", "declared in"), ("", "\n")]
        for path in record.declarations:
            fragments += [("class:option.path", f"  {path}"), ("", "\n")]
    return fragments


def _values(record: OptionRecord, width: int, values: OptionValues | None) -> StyleAndTextTuples:
    """Draw the `default` and the `example`, or say that they are on the way."""
    if values is None:
        return []
    known = values.known(record.name)
    if known is None:
        return [("", "\n"), ("class:option.pending", _PENDING), ("", "\n")]
    return _field("default", known.default, width) + _field("example", known.example, width)


def _field(label: str, value: Value | None, width: int) -> StyleAndTextTuples:
    """Draw one rendered field under its label, or nothing when there is none.

    **A failure is a line of this one field, and not of the pane.** An option
    whose default is an expression over a whole realized system cannot answer
    here, and that is the ordinary case rather than an error of `pynix`. The
    reader still gets the name, the type, the description and the example.
    """
    if value is None:
        return []
    fragments: StyleAndTextTuples = [("", "\n"), ("class:option.label", label), ("", "\n")]
    if value.error:
        return [*fragments, ("class:option.error", f"{_INDENT}does not evaluate: {value.error}"), ("", "\n")]
    if value.markdown:
        return [*fragments, *to_formatted_text(render_markdown(value.text, width)), ("", "\n")]
    return [*fragments, ("class:option.value", _indented(value.text)), ("", "\n")]


def _indented(text: str) -> str:
    """*text*, with every line under the label it belongs to."""
    return "\n".join(f"{_INDENT}{line}" for line in text.splitlines())


def source(
    records: Sequence[OptionRecord],
    subject: str,
    *,
    values: OptionValues | None = None,
) -> SearchSource[OptionRecord]:
    """Describe the options to the generic interface."""

    def draw(record: OptionRecord, width: int) -> StyleAndTextTuples:
        return detail(record, width, values)

    return SearchSource(
        items=records,
        rank=rank(records),
        row=lambda record: record.name,
        detail=draw,
        noun="option",
        subject=subject,
        style=STYLE,
        background=None if values is None else values.serve,
    )


async def browse(
    records: Sequence[OptionRecord],
    *,
    subject: str,
    initial_query: str = "",
    values: OptionValues | None = None,
) -> None:
    """Open the full-screen interface over *records*.

    *subject* is what the footer says the search covers, and *initial_query* is
    what the search bar holds when the interface opens.
    """
    await SearchTui(source(records, subject, values=values), initial_query=initial_query).run()
