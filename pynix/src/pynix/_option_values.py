"""Force one NixOS option's ``default`` and ``example``, when a reader asks.

``pynix._options`` walks a whole options tree and deliberately reads no
``default`` and no ``example``: the walk is one Nix list forced in one JSON
pass, so one option whose default cannot evaluate is the failure of all
24 941. That module says so, and that reason covers the bulk pass alone.

**This module is the other half, and it is one option at a time.**
``fetch_option_values`` gives a lazy attrset keyed by option name.
:class:`OptionValues` selects one key, forces it, and catches what it raises.
An "attribute X missing" failure -- the one ``builtins.tryEval`` explicitly
cannot catch -- crosses the binding boundary as an ordinary Python exception.
Measured through the bindings, one option at a time: a plain ``default =
false`` answers in 0.7 ms, ``default = throw "nope"`` arrives as
``ThrownError``, ``default = ({}).absent`` arrives as ``EvalError``, and the
eval session stays usable after both.

**The evaluator opens on the first request, and not before.** A warm search
reads its whole index from the cache and evaluates nothing, which is what the
cache exists for. Opening an evaluator when the interface opens would charge
every search about 5 s for a field that most searches never read. So
``open_tree`` is a function, this module calls it once, and the first reader
who selects an option that declares a default is the one who waits.

The module imports no ``prompt_toolkit``. :meth:`OptionValues.known` is what a
renderer calls, and :meth:`OptionValues.serve` is the background task that
answers it.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from nanopynix import AsyncValue


class EvaluatorUnavailableError(RuntimeError):
    """The evaluator could not be reached, so no option can answer.

    A failure of one option is a `NixError` from the force of that option.
    This one is the other kind: the target no longer evaluates, or it holds no
    options tree at all. The caller raises it from `open_tree`, and
    :class:`OptionValues` remembers it rather than evaluating again on every
    keypress.
    """


#: What the collector calls a value that is Markdown prose rather than Nix
#: source. ``lib.literalMD`` writes it, and a pane draws that text through the
#: Markdown renderer instead of as code.
LITERAL_MD = "literalMD"

#: The most rendered text this keeps for one field, in characters. A default
#: is a whole Nix value printed by ``lib.generators.toPretty``, and an option
#: whose default is a package set prints megabytes. The pane scrolls, so a
#: reader loses nothing they would have read.
LIMIT = 4000

#: What marks a value this cut at :data:`LIMIT`.
CUT = "\n..."

#: What Nix writes before each line of a report that names a cause.
_ERROR = "error:"


@dataclass(frozen=True)
class Value:
    """One rendered field of an option, or the reason it has none.

    An empty *error* means the field evaluated, and *text* is what it came
    to. A non-empty *error* means the field is declared and cannot evaluate,
    which the pane says in place of the value.
    """

    text: str = ""
    markdown: bool = False
    error: str = ""


@dataclass(frozen=True)
class Rendered:
    """The two fields of one option. ``None`` means the option declares none."""

    default: Value | None = None
    example: Value | None = None


def _short(text: str) -> str:
    """*text*, cut to :data:`LIMIT` characters."""
    return text if len(text) <= LIMIT else text[:LIMIT] + CUT


def _reason(exc: BaseException) -> str:
    """One line of *exc*, which is the line that names the failure.

    **Nix writes a report, and the root cause is the last `error:` line of
    it.** A thrown default arrives as five lines: `error:`, the builtin that
    was called, the source line, a caret, and then `error: this default is
    not available here`. The first line therefore says nothing at all, and
    the pane has one line to spend.

    A `NixError` also prints as `[ThrownError] ...` and carries the colour
    that Nix wrote. `msg_without_ansi` is the message alone.
    """
    text = exc.msg_without_ansi if isinstance(exc, NixError) else str(exc)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return exc.__class__.__name__
    for line in reversed(lines):
        head, marker, tail = line.partition(_ERROR)
        if marker and not head and tail.strip():
            return tail.strip()
    return lines[-1]


async def _field(entry: AsyncValue, which: str) -> Value | None:
    """Force one field of one option, and say what it came to.

    Each field is forced on its own, so a default that throws leaves a good
    example readable.
    """
    try:
        raw = await entry.attr(which).to_python()
    except NixError as exc:
        return Value(error=_reason(exc))
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return Value(error=f"the collector returned {type(raw).__name__} and not a rendered value")
    return Value(text=_short(str(raw.get("text", ""))), markdown=raw.get("type") == LITERAL_MD)


async def rendered(tree: AsyncValue, name: str) -> Rendered:
    """Force the ``default`` and the ``example`` of the option *name*.

    *tree* is what :func:`pynix._options.fetch_option_values` returned. Each
    field is forced on its own, so a default that throws still leaves a good
    example readable.
    """
    entry = tree.attr(name)
    return Rendered(default=await _field(entry, "default"), example=await _field(entry, "example"))


class OptionValues:
    """Answer "what is the default of this option", one option at a time.

    **The evaluator lives in the task that serves the requests.** anyio needs
    the task that enters a cancel scope to be the task that leaves it, and an
    eval session holds several. An `AsyncExitStack` shared between the render
    and this task therefore raised "Attempted to exit cancel scope in a
    different task than it was entered in" the moment the interface closed. So
    :meth:`serve` owns the stack, and the session closes with that task.
    """

    def __init__(self, open_tree: Callable[[], AbstractAsyncContextManager[AsyncValue]]) -> None:
        #: Open the evaluator and yield the lazy attrset of values. This runs
        #: once, on the first request that reaches the evaluator, and the
        #: session stays open until :meth:`serve` ends.
        self._open_tree = open_tree
        self._tree: AsyncValue | None = None
        self._known: dict[str, Rendered] = {}
        self._asked: str | None = None
        self._wake = anyio.Event()

        #: Why the evaluator could not open. It is the same failure every
        #: time, so this remembers it rather than paying the evaluation again
        #: on every keypress.
        self._broken = ""

    def known(self, name: str) -> Rendered | None:
        """What is known about *name*, and ask for it when nothing is.

        This is what a renderer calls, so it forces nothing and waits for
        nothing. `None` means "not resolved yet", and the pane says so.
        """
        found = self._known.get(name)
        if found is None and self._asked != name:
            self._asked = name
            self._wake.set()
        return found

    async def serve(self, redraw: Callable[[], None]) -> None:
        """Answer each request, and call *redraw* with each answer.

        The caller runs this beside the interface and cancels it when the
        interface closes. Only the newest request is served, because a reader
        who moves through ten options wants the tenth.
        """
        async with AsyncExitStack() as stack:
            while True:
                await self._wake.wait()
                self._wake = anyio.Event()
                name = self._asked
                if name is None or name in self._known:
                    continue
                self._known[name] = await self._resolve(stack, name)
                redraw()

    async def _resolve(self, stack: AsyncExitStack, name: str) -> Rendered:
        """Open the evaluator if it is not open, and read one option."""
        if not self._broken and self._tree is None:
            try:
                self._tree = await stack.enter_async_context(self._open_tree())
            except (NixError, EvaluatorUnavailableError) as exc:
                self._broken = _reason(exc)
        tree = self._tree
        if tree is None:
            failed = Value(error=self._broken)
            return Rendered(default=failed, example=failed)
        return await rendered(tree, name)
