"""The interactive Nix REPL: the prompt, the commands and the session.

``pynix/repl.py`` holds the command that starts this. The two are
separate modules because ``prompt_toolkit`` and the Nix grammar cost 91.8 ms,
and the parser loads every subcommand module on every start. ``pynix._impl`` says
why, and how the two halves reach each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import signal
import sys
import textwrap
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, override

import anyio
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from myst_parser.config.main import MdParserConfig
from myst_parser.parsers.mdit import create_md_parser
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from rich.console import Console, ConsoleOptions, JustifyMethod, RenderResult
from rich.containers import Renderables
from rich.markdown import (
    CodeBlock,
    Heading,
    Markdown,
    MarkdownContext,
    MarkdownElement,
    TextElement,
)
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table
from tree_sitter import Query, QueryCursor

import nanopynix
from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import EvaluatorAbandonedError, NixError
from nanopynix.verbosity import normalize_log_level
from pynix._completion import completion_prefix_at
from pynix._nix_syntax import NIX_GRAMMAR_PATH, NIX_LANGUAGE, parse_nix
from pynix._util import forward_nix_logs
from pynix._value_render import format_json
from pynix.repl import Repl
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    load_repl_target,
    repl_attr_search,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterable

    from prompt_toolkit.document import Document

    from nanopynix import AsyncReplSession

_PROMPT = "pynix> "
_COMMANDS = {
    ":a": "Add an attribute set to scope",
    ":add": "Add an attribute set to scope",
    ":b": "Build a derivation",
    ":build": "Build a derivation",
    ":d": "Show documentation of an expression",
    ":doc": "Show documentation of an expression",
    ":e": "Open a value's source in $EDITOR",
    ":edit": "Open a value's source in $EDITOR",
    ":exec": "Realise a Nix argv list and execute it",
    ":help": "Show this help",
    ":l": "Load a Nix file into scope",
    ":lf": "Load flake outputs into scope",
    ":ll": "Show the most recently loaded names",
    ":load": "Load a Nix file into scope",
    ":load-flake": "Load flake outputs into scope",
    ":p": "Evaluate and print an expression",
    ":print": "Evaluate and print an expression",
    ":q": "Exit the REPL",
    ":quit": "Exit the REPL",
    ":r": "Reload files and flakes",
    ":reload": "Reload files and flakes",
    ":run": "Build a derivation and run its main program",
    ":shell": "Realise a Nix string and execute it with the shell",
    ":t": "Show an expression's Nix type",
    ":type": "Show an expression's Nix type",
    ":verbosity": "Show or set Nix log verbosity",
    ":?": "Show this help",
}
_NIX_EXPRESSION_COMMANDS = frozenset(
    {
        ":a",
        ":add",
        ":b",
        ":build",
        ":d",
        ":doc",
        ":e",
        ":edit",
        ":exec",
        ":p",
        ":print",
        ":run",
        ":shell",
        ":t",
        ":type",
    }
)
_NIX_HIGHLIGHTS = Query(
    NIX_LANGUAGE,
    (Path(NIX_GRAMMAR_PATH) / "queries" / "highlights.scm").read_text(),
)
_NIX_HIGHLIGHT_STYLES = {
    # tree-sitter-nix 0.5.0 (numtide fork) renamed several capture groups to
    # align with nvim-treesitter conventions -- see its docs/highlight-groups.md
    # "Renames from v0.4.x and earlier" table. Both the surviving old names and
    # their v0.5.0 replacements are mapped here so this doesn't silently regress
    # on the next grammar bump either.
    "comment": "class:nix.comment",
    "comment.documentation": "class:nix.comment",
    "function": "class:nix.function",
    "function.call": "class:nix.function",
    "function.builtin": "class:nix.builtin",
    "keyword": "class:nix.keyword",
    "keyword.conditional": "class:nix.keyword",
    "keyword.operator": "class:nix.keyword",
    "keyword.import": "class:nix.keyword",
    "keyword.exception": "class:nix.keyword",
    "number": "class:nix.number",
    "number.float": "class:nix.number",
    "boolean": "class:nix.builtin",
    "constant.builtin": "class:nix.builtin",
    "operator": "class:nix.operator",
    # v0.5.0 dropped the old blanket "any identifier" fallback that used to
    # ride along under @property -- @variable is now the only thing covering
    # plain variable references, so it needs its own style to avoid going dark.
    "variable": "class:nix.variable",
    "variable.member": "class:nix.property",
    "punctuation.bracket": "class:nix.punctuation",
    "punctuation.delimiter": "class:nix.punctuation",
    "punctuation.special": "class:nix.punctuation",
    "string": "class:nix.string",
    "string.escape": "class:nix.escape",
    "string.special.path": "class:nix.path",
    "string.special.url": "class:nix.path",
    "variable.parameter": "class:nix.parameter",
    "variable.parameter.builtin": "class:nix.parameter",
}
_REPL_STYLE = Style.from_dict(
    {
        "nix.builtin": "ansicyan",
        "nix.comment": "ansibrightblack italic",
        "nix.escape": "ansiyellow",
        "nix.function": "ansiblue",
        "nix.keyword": "ansimagenta bold",
        "nix.number": "ansicyan",
        "nix.operator": "ansimagenta",
        "nix.parameter": "ansiyellow",
        "nix.path": "underline ansiblue",
        "nix.property": "ansiblue",
        "nix.punctuation": "ansibrightblack",
        "nix.string": "ansigreen",
        "nix.variable": "ansiblue",
    },
)
_HELP_ROWS = (
    ("<expr>", "Evaluate and print an expression"),
    ("<name> = <expr>", "Bind an expression to a name"),
    (":a, :add <expr>", "Add an attrset's attributes to scope"),
    (":b, :build <expr>", "Build an evaluated derivation"),
    (":d, :doc <expr>", "Show documentation of an expression"),
    (":e, :edit <expr>", "Open a package, function, path, or string in $EDITOR"),
    (":l, :load <path>", "Load a Nix file and add its attributes to scope"),
    (":lf, :load-flake <ref>", "Load flake outputs into scope"),
    (":ll, :last-loaded", "Show names from the most recent load"),
    (":p, :print <expr>", "Evaluate and print an expression"),
    (":run <expr>", "Build a derivation and run its main program"),
    (":exec <list>", "Realise a Nix argv list and execute it"),
    (":shell <expr>", "Realise a Nix string and execute it with the shell"),
    (":r, :reload", "Reload files and flakes"),
    (":t, :type <expr>", "Show an expression's Nix type"),
    (":verbosity [level]", "Show or set Nix log verbosity (0-7 or a level name)"),
    (":q, :quit", "Exit the REPL"),
    (":?, :help", "Show this help"),
)


def _render_help() -> str:
    """Render the REPL help with Rich's terminal-aware column layout."""
    output = StringIO()
    console = Console(file=output, color_system=None)
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column()
    for command, description in _HELP_ROWS:
        table.add_row(command, description)
    console.print("Commands:")
    console.print(table)
    return "\n".join(line.rstrip() for line in output.getvalue().splitlines())


_HELP = _render_help()


class _LeftHeading(Heading):
    """A heading that stays aligned to the left margin."""

    LEVEL_ALIGN: ClassVar[dict[str, JustifyMethod]] = {
        f"h{i}": "left"
        for i in range(1, 7)  # type: ignore[misc] -- rich TypedDict JustifyMethod is Literal
    }


class _NixCodeBlock(CodeBlock):
    """A code block with syntax highlighting and a left border bar."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        lexer = "nix" if self.lexer_name in ("text", "") else self.lexer_name
        syntax = Syntax(
            code,
            lexer,
            theme="ansi_dark",
            background_color="default",
            word_wrap=True,
            line_numbers=False,
        )
        bar_style = console.get_style("dim", default="none")
        bar_prefix = Segment("  │ ", bar_style)
        new_line = Segment("\n")
        render_options = options.update(width=max(options.max_width - 4, 20))
        lines = console.render_lines(syntax, render_options)
        for line in lines:
            yield bar_prefix
            yield from line
            yield new_line


class _DefList(MarkdownElement):
    new_line = True


class _DefTerm(TextElement):
    new_line = True
    style_name = "bold"

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.text


class _DefDesc(MarkdownElement):
    new_line = True

    def __init__(self) -> None:
        self.elements: Renderables = Renderables()
        super().__init__()

    def on_child_close(self, context: MarkdownContext, child: MarkdownElement) -> bool:
        del context
        self.elements.append(child)
        return False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        render_options = options.update(width=max(options.max_width - 4, 20))
        lines = console.render_lines(self.elements, render_options)
        prefix = Segment("  : ")
        indent = Segment("    ")
        new_line = Segment("\n")
        first = True
        for line in lines:
            yield prefix if first else indent
            yield from line
            yield new_line
            first = False


class _ColonFence(MarkdownElement):
    """Render MyST colon fence container (:::{...} ... :::)."""

    new_line = True

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> _ColonFence:  # noqa: ARG003 -- MarkdownElement protocol requires the parameter name markdown
        return cls(token.content)

    def __init__(self, content: str) -> None:
        self.content = content
        super().__init__()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.content:
            yield _NixMarkdown(self.content)


class _NixMarkdown(Markdown):
    """Markdown customized for Nix and MyST documentation."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
        "fence": _NixCodeBlock,
        "code_block": _NixCodeBlock,
        "colon_fence": _ColonFence,
        "dl_open": _DefList,
        "dt_open": _DefTerm,
        "dd_open": _DefDesc,
    }

    def __init__(self, markup: str, **kwargs: Any) -> None:
        super().__init__(markup, **kwargs)
        config = MdParserConfig(
            enable_extensions={
                "colon_fence",
                "deflist",
                "strikethrough",
                "tasklist",
            }
        )
        parser = create_md_parser(config, RendererHTML)
        tokens = parser.parse(markup)
        for t in tokens:
            if t.type == "colon_fence":
                t.tag = ""
        self.parsed = tokens


def _render_markdown(text: str) -> ANSI:
    """Render Markdown into formatted ANSI text bounded by the longest line."""
    lines = text.splitlines()
    max_line = max((len(line.rstrip()) for line in lines), default=80)
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    render_width = min(max(max_line + 4, 60), terminal_width)

    output = StringIO()
    console = Console(file=output, force_terminal=True, width=render_width)
    console.print(_NixMarkdown(text))
    return ANSI(output.getvalue().rstrip("\n"))


def _repl_history() -> FileHistory:
    """Create persistent REPL history in the XDG state directory."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = state_home / "pynix" / "repl_history"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(path))


def _nix_input(text: str) -> tuple[str, int] | None:
    """Return Nix source and its character offset within one REPL input line."""
    command, separator, argument = text.partition(" ")
    if command.startswith(":"):
        if command not in _NIX_EXPRESSION_COMMANDS or not separator:
            return None
        return argument, len(command) + len(separator)
    return text, 0


class _NixLexer(Lexer):
    """Color Nix expressions with the tree-sitter-nix highlight query."""

    @override
    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        source_and_offset = _nix_input(document.text)
        styles = [""] * len(document.text)
        if source_and_offset is not None:
            source, offset = source_and_offset
            encoded = source.encode()
            captures = QueryCursor(_NIX_HIGHLIGHTS).captures(parse_nix(source).root_node)
            for capture, nodes in captures.items():
                style = _NIX_HIGHLIGHT_STYLES.get(capture)
                if style is None:
                    continue
                for node in nodes:
                    start = offset + len(encoded[: node.start_byte].decode())
                    end = offset + len(encoded[: node.end_byte].decode())
                    styles[start:end] = [style] * (end - start)

        def get_line(line_number: int) -> StyleAndTextTuples:
            line = document.lines[line_number]
            start = sum(len(previous) + 1 for previous in document.lines[:line_number])
            fragments: StyleAndTextTuples = []
            for character, style in zip(line, styles[start : start + len(line)], strict=True):
                if fragments and fragments[-1][0] == style:
                    fragments[-1] = (style, fragments[-1][1] + character)
                else:
                    fragments.append((style, character))
            return fragments

        return get_line


class _ReplCompleter(Completer):
    """Complete REPL commands and names from the current Nix lexical scope."""

    def __init__(self, repl: AsyncReplSession[Any]) -> None:
        self._repl = repl

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        """Prompt-toolkit requires this synchronous hook even for async completion."""
        del document, complete_event
        return ()

    async def get_completions_async(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> AsyncGenerator[Completion]:
        before_cursor = document.text_before_cursor
        if not before_cursor:
            # **Tab, and not typing.** The shell convention is that Tab on an
            # empty word offers everything that can go there, and `nix repl`
            # follows it. `complete_while_typing` is on, so a completer that
            # answers an empty line unasked would open the menu as soon as the
            # prompt appeared. `completion_requested` is true for a keypress
            # and false for typing, which is exactly that difference.
            #
            # No cap on the number of names. The menu of prompt-toolkit
            # scrolls, so a large scope fills a bounded box rather than the
            # terminal, and the branch below already yields every name that
            # starts with one letter without a cap of its own.
            if complete_event.completion_requested:
                for name in await self._repl.scope_names():
                    yield Completion(name, start_position=0)
            return
        command, separator, _argument = before_cursor.partition(" ")
        if not separator and command.startswith(":"):
            for name, description in _COMMANDS.items():
                if name.startswith(command):
                    yield Completion(name, start_position=-len(command), display_meta=description)
            return

        async for item in self._name_completions(before_cursor, complete_event):
            yield item

    async def _name_completions(
        self,
        before_cursor: str,
        complete_event: CompleteEvent,
    ) -> AsyncGenerator[Completion]:
        """Each name of the scope, or of an attribute set, that continues the last word.

        A branch of its own, and not of the caller: the three cases together
        cost more complexity than the strict lint allows one function.
        """
        source_and_offset = _nix_input(before_cursor)
        if source_and_offset is None:
            return
        source, _offset = source_and_offset
        if not source:
            # An expression command with nothing typed after the space (":e ")
            # is a bare position, like an empty line. Complete from the ambient
            # scope on a keypress, and not while typing: `complete_while_typing`
            # is on, so answering the space itself would pop the menu open the
            # moment it is typed.
            if complete_event.completion_requested:
                for name in await self._repl.scope_names():
                    yield Completion(name, start_position=0)
            return
        target = completion_prefix_at(source, len(source.encode()))
        if target is None:
            return
        prefix, partial = target
        candidates = await self._repl.scope_names() if prefix is None else await self._attr_names(prefix)
        for name in candidates:
            if name.startswith(partial):
                yield Completion(name, start_position=-len(partial))

    async def _attr_names(self, expression: str) -> list[str]:
        try:
            value = await self._repl.string(expression)
            try:
                return await value.attr_names()
            finally:
                await value.release()
        except NixError:
            return []


class _ReplState:
    """Mutable state threaded through one REPL session's command handlers."""

    def __init__(
        self,
        repl: AsyncReplSession[Any],
        line_editors: tuple[str, ...],
        loaded: list[tuple[str, str]],
        last_loaded: list[str],
    ) -> None:
        self.repl = repl
        self.line_editors = line_editors
        self.loaded = loaded
        self.last_loaded = last_loaded

    async def add_attrs(self, value: Any) -> list[str]:
        self.last_loaded = await self.repl.add_attrs(value)
        print_formatted_text(f"Added {len(self.last_loaded)} variables: {' '.join(self.last_loaded)}")
        return self.last_loaded

    async def evaluate(self, expr: str) -> Any:
        value = await self.repl.string(expr)
        print_formatted_text(format_json(await value.to_python()))
        return value

    async def reload_sources(self) -> None:
        await self.repl.reset_file_cache()
        for load_command, source in self.loaded:
            value = await (self.repl.load_file(source) if load_command == ":load" else self.repl.eval_flake(source))
            await self.add_attrs(value)


async def _cmd_print(state: _ReplState, argument: str) -> None:
    await state.evaluate(argument)


async def _cmd_type(state: _ReplState, argument: str) -> None:
    value = await state.repl.string(argument)
    print_formatted_text((await value.get_type()).name.lower())


async def _cmd_build(state: _ReplState, argument: str) -> None:
    value = await state.repl.string(argument)
    outputs = await value.build()
    print_formatted_text(format_json(outputs))


async def _cmd_doc(state: _ReplState, argument: str) -> None:
    value = await state.repl.string(argument)
    doc = await value.get_doc()
    if doc is not None:
        doc_text = textwrap.dedent(doc.doc).strip()
        if doc.args and doc.name:
            args = " ".join(f"*{arg}*" for arg in doc.args)
            md = f"**Synopsis:** `builtins.{doc.name}` {args}\n\n{doc_text}"
        else:
            md = doc_text
        print_formatted_text(_render_markdown(md))
        return

    sel = await state.repl.repl_select(argument)
    if sel is not None:
        name, parent_attrs = sel
        attr = await parent_attrs.attr_doc(name)
        if attr is not None:
            pos_str = f"{attr.path}:{attr.line}"
            comment = attr.doc if attr.doc is not None else "No documentation found."
            md = f"Attribute `{name}`\n\n  … defined at {pos_str}\n\n{comment}".strip()
            print_formatted_text(_render_markdown(md))
            return

    raise ReplRunError("value does not have documentation")


async def _cmd_edit(state: _ReplState, argument: str) -> None:
    await _edit(await state.repl.string(argument), state.line_editors)
    await state.reload_sources()


async def _cmd_run(state: _ReplState, argument: str) -> None:
    await _run_derivation(await state.repl.string(argument))


async def _cmd_exec(state: _ReplState, argument: str) -> None:
    await _exec_argv(await (await state.repl.string(argument)).realise_argv())


async def _cmd_shell(state: _ReplState, argument: str) -> None:
    await _shell(await (await state.repl.string(argument)).realise_string())


async def _cmd_add(state: _ReplState, argument: str) -> None:
    await state.add_attrs(await state.repl.string(argument))


async def _cmd_load(state: _ReplState, argument: str) -> None:
    await state.add_attrs(await state.repl.load_file(argument))
    state.loaded.append((":load", argument))


async def _cmd_load_flake(state: _ReplState, argument: str) -> None:
    await state.add_attrs(await state.repl.eval_flake(argument))
    state.loaded.append((":load-flake", argument))


async def _cmd_last_loaded(state: _ReplState, argument: str) -> None:
    del argument
    print_formatted_text(" ".join(state.last_loaded) if state.last_loaded else "nothing has been loaded")


async def _cmd_reload(state: _ReplState, argument: str) -> None:
    del argument
    await state.reload_sources()


async def _cmd_help(state: _ReplState, argument: str) -> None:
    del state, argument
    print_formatted_text(_HELP)


async def _cmd_verbosity(state: _ReplState, argument: str) -> None:
    """Show or set the level this repl's evaluator logs at.

    Scoped to this repl, and not to the process. A store operation this repl
    runs still logs at the session's level, and so do the threads Nix starts
    for itself, such as a substituter.
    """
    try:
        verbosity = await (
            state.repl.get_verbosity() if not argument else state.repl.set_verbosity(normalize_log_level(argument))
        )
    except (TypeError, ValueError) as exc:
        print_formatted_text(f"error: {exc}")
    else:
        print_formatted_text(f"{verbosity.name.lower()} ({int(verbosity)})")


_COMMAND_HANDLERS: dict[str, Callable[[_ReplState, str], Awaitable[None]]] = {
    ":p": _cmd_print,
    ":print": _cmd_print,
    ":t": _cmd_type,
    ":type": _cmd_type,
    ":b": _cmd_build,
    ":build": _cmd_build,
    ":d": _cmd_doc,
    ":doc": _cmd_doc,
    ":e": _cmd_edit,
    ":edit": _cmd_edit,
    ":run": _cmd_run,
    ":exec": _cmd_exec,
    ":shell": _cmd_shell,
    ":a": _cmd_add,
    ":add": _cmd_add,
    ":l": _cmd_load,
    ":load": _cmd_load,
    ":lf": _cmd_load_flake,
    ":load-flake": _cmd_load_flake,
    ":ll": _cmd_last_loaded,
    ":last-loaded": _cmd_last_loaded,
    ":r": _cmd_reload,
    ":reload": _cmd_reload,
    ":help": _cmd_help,
    ":?": _cmd_help,
    ":verbosity": _cmd_verbosity,
}


async def _run_repl_loop(
    repl: AsyncReplSession[Any],
    prompt: Any,
    *,
    initial_loaded: list[str] | None = None,
    initial_sources: list[tuple[str, str]] | None = None,
    line_editors: tuple[str, ...] = (),
) -> None:
    """Read and evaluate lines until the user exits the REPL."""
    state = _ReplState(
        repl=repl,
        line_editors=line_editors,
        loaded=list(initial_sources or []),
        last_loaded=list(initial_loaded or []),
    )

    print_formatted_text(_HELP)
    while True:
        try:
            line = await prompt.prompt_async(_PROMPT)
        except EOFError:
            print_formatted_text("")
            return
        except KeyboardInterrupt:
            print_formatted_text("^C")
            continue

        text = line.strip()
        if not text:
            continue
        command, _, argument = text.partition(" ")
        if command in {":quit", ":q"}:
            return

        try:
            with _interruptible() as scope:
                await _dispatch(state, repl, command, argument, line)
            if scope.cancel_called:
                print_formatted_text("^C")
        except EvaluatorAbandonedError as exc:
            # Ctrl-C reached an evaluation that Nix cannot stop, so the
            # evaluator thread is abandoned. Every later expression raises this
            # same error, because the evaluator is gone for the life of the
            # process. A prompt that only repeats the error is worse than no
            # prompt, so leave, and say what happened.
            print_formatted_text(f"error: {exc}")
            print_formatted_text("this evaluator cannot be used again; start pynix again")
            return
        except (NixError, ReplRunError) as exc:
            _print_error(exc)


async def _dispatch(
    state: _ReplState,
    repl: AsyncReplSession[Any],
    command: str,
    argument: str,
    line: str,
) -> None:
    """Run one REPL input: a command, or an expression to evaluate and print."""
    handler = _COMMAND_HANDLERS.get(command)
    if handler is not None:
        await handler(state, argument)
    elif command.startswith(":"):
        print_formatted_text(f"unknown command: {command}; try :help")
    else:
        value = await repl.line(line)
        if value is not None:
            print_formatted_text(format_json(await value.to_python()))


@contextlib.contextmanager
def _interruptible() -> Generator[anyio.CancelScope]:
    """Let Ctrl-C stop the running evaluation and return to the prompt.

    Cancelling the scope is all this needs to do. ``NixThreadExecutor`` turns a
    cancellation into Nix's own per-thread interrupt, so the evaluator thread
    stops instead of running on unwatched. Before that existed, Ctrl-C here
    cancelled the REPL's task and left Nix evaluating.

    ``loop.add_signal_handler`` rather than ``signal.signal``, because the
    handler must run on the event loop and cancel a scope. The arm at
    ``prompt_async`` above stays: that one covers Ctrl-C while the user types,
    where prompt_toolkit owns the terminal and no evaluation is in flight.

    Nix answers an interrupt only where it polls ``checkInterrupt()``. A fetch,
    a store operation and a build stop. A pure evaluation does not, and there
    the evaluator is abandoned -- so restore the default handler first, and a
    second Ctrl-C leaves the REPL rather than doing nothing.
    """
    loop = asyncio.get_running_loop()
    with anyio.CancelScope() as scope:
        try:
            loop.add_signal_handler(signal.SIGINT, scope.cancel)
        except NotImplementedError:
            # No signal handlers on this loop (Windows, or a non-main thread).
            # The REPL still works; Ctrl-C just keeps its old behaviour.
            yield scope
            return
        try:
            yield scope
        finally:
            loop.remove_signal_handler(signal.SIGINT)


def _print_error(exc: NixError | ReplRunError) -> None:
    """Print a REPL error, preserving Nix's ANSI diagnostics."""
    if isinstance(exc, NixError):
        print_formatted_text(ANSI(exc.msg))
    else:
        print_formatted_text(f"error: {exc}")


async def _load_initial_target(repl: AsyncReplSession[Any], target: EvaluationTarget) -> list[str]:
    """Load a command-line target into the persistent REPL scope."""
    if target.file is None and target.flake is None:
        return []
    value = await load_repl_target(target, repl, attr_search=repl_attr_search())
    names = await repl.add_attrs(value)
    print_formatted_text(f"Added {len(names)} variables: {' '.join(names)}")
    return names


class ReplRunError(RuntimeError):
    """A command could not be selected or started by the REPL."""


async def _run_derivation(value: Any) -> int:
    """Build *value* and run it using Nix's derivation executable convention."""
    main_program, warning = await _main_program(value)
    if warning is not None:
        print_formatted_text(f"warning: {warning}")

    outputs = await value.build()
    out_path = _primary_output(outputs)
    program = Path(out_path) / "bin" / main_program
    try:
        process = await anyio.open_process([str(program)], stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot run {program}: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: {program} exited with status {return_code}")
    return return_code


def _editor_argv(path: str, line: int, line_editors: tuple[str, ...]) -> list[str]:
    """Match Nix's ``editorFor`` argument construction with configured editors."""
    editor = os.environ.get("EDITOR", "cat")
    try:
        argv = shlex.split(editor)
    except ValueError as exc:
        raise ReplRunError(f"cannot parse $EDITOR: {exc}") from exc
    if not argv:
        raise ReplRunError("$EDITOR is empty")
    if line > 0 and any(name in editor for name in line_editors):
        argv.append(f"+{line}")
    argv.append(path)
    return argv


async def _edit(value: Any, line_editors: tuple[str, ...]) -> int:
    """Open the Nix-selected source location for *value* in the user's editor."""
    path, line = await value.edit_location()
    argv = _editor_argv(path, line, line_editors)
    try:
        process = await anyio.open_process(argv, stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot start editor {argv[0]!r}: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: editor exited with status {return_code}")
    return return_code


async def _exec_argv(argv: list[str]) -> int:
    """Execute a realised Nix argv list without shell parsing."""
    if not argv:
        raise ReplRunError(":exec command list is empty")
    try:
        process = await anyio.open_process(argv, stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot run {argv[0]!r}: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: {argv[0]} exited with status {return_code}")
    return return_code


def _primary_output(outputs: dict[str, str]) -> str:
    out_path = outputs.get("out")
    if out_path is None:
        raise ReplRunError("derivation has no 'out' output")
    return out_path


async def _shell(command: str) -> int:
    """Execute a realised Nix string with the user's shell."""
    if not command:
        raise ReplRunError(":shell command is empty")
    try:
        process = await anyio.open_process(command, stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot start shell: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: shell exited with status {return_code}")
    return return_code


async def _main_program(value: Any) -> tuple[str, str | None]:
    """Select a derivation executable using the same order as ``nix run``."""
    if await value.has_attr("meta"):
        meta = value.attr("meta")
        if await meta.has_attr("mainProgram"):
            main_program = await meta.attr("mainProgram").to_python()
            if not isinstance(main_program, str):
                raise ReplRunError("derivation meta.mainProgram is not a string")
            return main_program, None

    if await value.has_attr("pname"):
        pname = await value.attr("pname").to_python()
        if not isinstance(pname, str):
            raise ReplRunError("derivation pname is not a string")
        return pname, f"derivation has no meta.mainProgram; using pname {pname!r}"

    if not await value.has_attr("name"):
        raise ReplRunError("derivation has neither meta.mainProgram, pname, nor name")
    name = await value.attr("name").to_python()
    if not isinstance(name, str):
        raise ReplRunError("derivation name is not a string")
    main_program = _derivation_name_part(name)
    return main_program, f"derivation has no meta.mainProgram; using name {main_program!r}"


def _derivation_name_part(name: str) -> str:
    """Match Nix's ``DrvName(name).name`` split rule."""
    for index, char in enumerate(name[:-1]):
        if char == "-" and not name[index + 1].isalpha():
            return name[:index]
    return name


async def run_repl(command: Repl) -> None:
    """Open the session, and drive the prompt until the user leaves it.

    The body of ``Repl.run``. It lives here, and not beside the command class,
    because ``prompt_toolkit`` and the Nix grammar are 91.8 ms that every
    ``pynix`` start paid. See the docstring of ``pynix._impl``.
    """
    target = EvaluationTarget.from_command(command)
    try:
        target.validate()
    except EvaluationTargetError as exc:
        print_formatted_text(f"error: {exc}")
        raise SystemExit(1) from exc

    # The REPL keeps an RPC session. A REPL runs new expressions from the user
    # repeatedly. Process isolation ensures that an unrecoverable evaluator fault
    # or abandoned evaluator thread does not crash the interactive prompt.
    async with nanopynix.rpc.Session(experimental_features=["flakes", "nix-command"]) as nix:
        with patch_stdout():
            async with (
                forward_nix_logs(nix, log_file=sys.stdout),
                nix.store(command.store) as store,
                nix.repl(store) as repl,
            ):
                prompt: PromptSession[str] = PromptSession(
                    completer=_ReplCompleter(repl),
                    complete_while_typing=True,
                    history=_repl_history(),
                    lexer=_NixLexer(),
                    style=_REPL_STYLE,
                )
                try:
                    initial_loaded = await _load_initial_target(repl, target)
                except EvaluationTargetError as exc:
                    print_formatted_text(f"error: {exc}")
                    raise SystemExit(1) from exc
                initial_sources: list[tuple[str, str]] = []
                if target.file is not None:
                    initial_sources.append((":load", target.file))
                elif target.flake is not None:
                    initial_sources.append((":load-flake", target.flake))
                await _run_repl_loop(
                    repl,
                    prompt,
                    initial_loaded=initial_loaded,
                    initial_sources=initial_sources,
                    line_editors=repl.line_editors,
                )
