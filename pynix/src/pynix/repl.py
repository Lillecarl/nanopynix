"""Interactive Nix REPL command."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, override

import tree_sitter_nix  # type: ignore[reportMissingTypeStubs] -- tree-sitter-nix does not ship type stubs
from clypi import Command, arg
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from tree_sitter import Language, Parser, Query, QueryCursor

import nanopynix
from nanopynix.exceptions import NixError
from nanopynix.verbosity import normalize_log_level
from pynix._util import forward_nix_logs, prepare_sys_path
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    file_option,
    flake_option,
    load_repl_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from prompt_toolkit.document import Document

    from nanopynix import ReplSession

_DEFAULT_STORE = "auto"
_PROMPT = "pynix> "
_COMMANDS = {
    ":a": "Add an attribute set to scope",
    ":add": "Add an attribute set to scope",
    ":b": "Build a derivation",
    ":build": "Build a derivation",
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
_NIX_EXPRESSION_COMMANDS = frozenset({":a", ":add", ":b", ":build", ":e", ":edit", ":exec", ":p", ":print", ":run", ":shell", ":t", ":type"})
class _Grammar(Protocol):
    path: str | None


class _GrammarConfig(Protocol):
    grammars: list[_Grammar]


_NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))  # type: ignore[reportDeprecated, reportUnknownMemberType] -- tree-sitter-nix 0.3 exposes the legacy integer language pointer
_NIX_GRAMMAR_CONFIG = cast("_GrammarConfig | None", tree_sitter_nix.config())  # type: ignore[reportUnknownMemberType] -- tree-sitter-nix has no type stubs
if _NIX_GRAMMAR_CONFIG is None:
    raise RuntimeError("tree-sitter-nix did not provide its grammar configuration")
_NIX_GRAMMAR_PATH = _NIX_GRAMMAR_CONFIG.grammars[0].path
if _NIX_GRAMMAR_PATH is None:
    raise RuntimeError("tree-sitter-nix did not provide its grammar path")
_NIX_HIGHLIGHTS = Query(
    _NIX_LANGUAGE,
    (Path(_NIX_GRAMMAR_PATH) / "queries" / "highlights.scm").read_text(),
)
_NIX_HIGHLIGHT_STYLES = {
    "comment": "class:nix.comment",
    "escape": "class:nix.escape",
    "function": "class:nix.function",
    "function.builtin": "class:nix.builtin",
    "keyword": "class:nix.keyword",
    "number": "class:nix.number",
    "operator": "class:nix.operator",
    "property": "class:nix.property",
    "punctuation.bracket": "class:nix.punctuation",
    "punctuation.delimiter": "class:nix.punctuation",
    "punctuation.special": "class:nix.punctuation",
    "string": "class:nix.string",
    "string.special.path": "class:nix.path",
    "string.special.uri": "class:nix.path",
    "variable.builtin": "class:nix.builtin",
    "variable.parameter": "class:nix.parameter",
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
    }
)
_HELP = """Commands:
  <expr>                 Evaluate and print an expression
  <name> = <expr>        Bind an expression to a name
  :a, :add <expr>        Add an attrset's attributes to scope
  :b, :build <expr>      Build an evaluated derivation
  :e, :edit <expr>       Open a package, function, path, or string in $EDITOR
  :l, :load <path>       Load a Nix file and add its attributes to scope
  :lf, :load-flake <ref> Load flake outputs into scope
  :ll, :last-loaded      Show names from the most recent load
  :p, :print <expr>      Evaluate and print an expression
  :run <expr>             Build a derivation and run its main program
  :exec <list>            Realise a Nix argv list and execute it
  :shell <expr>           Realise a Nix string and execute it with the shell
  :r, :reload            Reload files and flakes
  :t, :type <expr>       Show an expression's Nix type
  :verbosity [level]      Show or set Nix log verbosity (0-7 or a level name)
  :q, :quit              Exit the REPL
  :?, :help              Show this help"""


def _nix_input(text: str) -> tuple[str, int] | None:
    """Return Nix source and its character offset within one REPL input line."""
    command, separator, argument = text.partition(" ")
    if command.startswith(":"):
        if command not in _NIX_EXPRESSION_COMMANDS or not separator:
            return None
        return argument, len(command) + len(separator)
    return text, 0


def _parse_nix(source: str):
    """Parse one possibly incomplete Nix expression for prompt interaction."""
    return Parser(_NIX_LANGUAGE).parse(source.encode())


def _nodes(root: Any) -> list[Any]:
    """Return all nodes below ``root`` in source order."""
    nodes: list[Any] = [root]
    result: list[Any] = []
    while nodes:
        node = nodes.pop()
        result.append(node)
        nodes.extend(reversed(node.children))
    return result


def _completion_target(source: str) -> tuple[str | None, str] | None:
    """Return the semantic prefix and partial identifier at the cursor, if any."""
    encoded = source.encode()
    nodes = _nodes(_parse_nix(source).root_node)
    selections = [node for node in nodes if node.type == "select_expression" and node.end_byte == len(encoded)]
    if selections:
        target = selections[-1]
        attrpath = target.child_by_field_name("attrpath")
        if attrpath is None:
            return None
        attributes = [node for node in attrpath.named_children if node.type == "identifier"]
        if not attributes:
            return None
        partial_node = attributes[-1]
        prefix = encoded[target.start_byte : partial_node.start_byte].rstrip(b".").decode()
        partial = encoded[partial_node.start_byte : partial_node.end_byte].decode()
        return (prefix, partial) if prefix else None

    if source.endswith("."):
        before_dot = source.removesuffix(".")
        encoded_before_dot = before_dot.encode()
        expressions = [
            node
            for node in _nodes(_parse_nix(before_dot).root_node)
            if node.type in {"identifier", "select_expression", "variable_expression"}
            and node.end_byte == len(encoded_before_dot)
        ]
        if expressions:
            target = expressions[-1]
            return encoded_before_dot[target.start_byte : target.end_byte].decode(), ""

    variables = [node for node in nodes if node.type == "variable_expression" and node.end_byte == len(encoded)]
    if variables:
        target = variables[-1]
        name = target.child_by_field_name("name")
        if name is not None:
            return None, encoded[name.start_byte : name.end_byte].decode()
    identifiers = [node for node in nodes if node.type == "identifier" and node.end_byte == len(encoded)]
    if identifiers:
        target = identifiers[-1]
        return None, encoded[target.start_byte : target.end_byte].decode()

    interpolation_starts = [node for node in nodes if node.type == "${" and node.end_byte == len(encoded)]
    if interpolation_starts:
        return None, ""
    return None


class _NixLexer(Lexer):
    """Color Nix expressions with the tree-sitter-nix highlight query."""

    @override
    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        source_and_offset = _nix_input(document.text)
        styles = [""] * len(document.text)
        if source_and_offset is not None:
            source, offset = source_and_offset
            encoded = source.encode()
            captures = QueryCursor(_NIX_HIGHLIGHTS).captures(_parse_nix(source).root_node)
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

    def __init__(self, repl: ReplSession) -> None:
        self._repl = repl

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        """Prompt-toolkit requires this synchronous hook even for async completion."""
        del document, complete_event
        return ()

    async def get_completions_async(self, document: Document, complete_event: CompleteEvent):
        del complete_event
        before_cursor = document.text_before_cursor
        command, separator, _argument = before_cursor.partition(" ")
        if not separator and command.startswith(":"):
            for name, description in _COMMANDS.items():
                if name.startswith(command):
                    yield Completion(name, start_position=-len(command), display_meta=description)
            return

        source_and_offset = _nix_input(before_cursor)
        if source_and_offset is not None:
            source, _offset = source_and_offset
            target = _completion_target(source)
            if target is not None:
                prefix, partial = target
                candidates = await self._repl.scope_names() if prefix is None else await self._attr_names(prefix)
                for name in candidates:
                    if name.startswith(partial):
                        yield Completion(name, start_position=-len(partial))
                return

    async def _attr_names(self, expression: str) -> list[str]:
        try:
            value = await self._repl.string(expression)
            try:
                return await value.attr_names()
            finally:
                await value.release()
        except NixError:
            return []


async def _run_repl_loop(
    repl: ReplSession,
    prompt: Any,
    *,
    initial_loaded: list[str] | None = None,
    initial_sources: list[tuple[str, str]] | None = None,
    line_editors: tuple[str, ...] = (),
) -> None:
    """Read and evaluate lines until the user exits the REPL."""
    loaded = list(initial_sources or [])
    last_loaded = list(initial_loaded or [])

    async def add_attrs(value: Any) -> list[str]:
        nonlocal last_loaded
        last_loaded = await repl.add_attrs(value)
        print_formatted_text(f"Added {len(last_loaded)} variables: {' '.join(last_loaded)}")
        return last_loaded

    async def evaluate(expr: str) -> Any:
        value = await repl.string(expr)
        print_formatted_text(json.dumps(await value.force_json(), indent=2, sort_keys=True))
        return value

    async def reload_sources() -> None:
        await repl.reset_file_cache()
        for load_command, source in loaded:
            value = await (repl.load_file(source) if load_command == ":load" else repl.eval_flake(source))
            await add_attrs(value)

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
        if command in {":help", ":?"}:
            print_formatted_text(_HELP)
            continue
        if command == ":verbosity":
            try:
                verbosity = await (repl.get_verbosity() if not argument else repl.set_verbosity(normalize_log_level(argument)))
            except (TypeError, ValueError) as exc:
                print_formatted_text(f"error: {exc}")
            else:
                print_formatted_text(f"{verbosity.name.lower()} ({int(verbosity)})")
            continue

        try:
            if command in {":p", ":print"}:
                await evaluate(argument)
                continue
            if command in {":t", ":type"}:
                value = await repl.string(argument)
                print_formatted_text((await value.get_type()).name.lower())
                continue
            if command in {":b", ":build"}:
                value = await repl.string(argument)
                outputs = await value.build()
                print_formatted_text(json.dumps(outputs, indent=2, sort_keys=True))
                continue
            if command in {":e", ":edit"}:
                await _edit(await repl.string(argument), line_editors)
                await reload_sources()
                continue
            if command == ":run":
                await _run_derivation(await repl.string(argument))
                continue
            if command == ":exec":
                await _exec_argv(await (await repl.string(argument)).realise_argv())
                continue
            if command == ":shell":
                await _shell(await (await repl.string(argument)).realise_string())
                continue
            if command in {":a", ":add"}:
                await add_attrs(await repl.string(argument))
                continue
            if command in {":l", ":load"}:
                await add_attrs(await repl.load_file(argument))
                loaded.append((":load", argument))
                continue
            if command in {":lf", ":load-flake"}:
                await add_attrs(await repl.eval_flake(argument))
                loaded.append((":load-flake", argument))
                continue
            if command in {":ll", ":last-loaded"}:
                print_formatted_text(" ".join(last_loaded) if last_loaded else "nothing has been loaded")
                continue
            if command in {":r", ":reload"}:
                await reload_sources()
                continue
            if command.startswith(":"):
                print_formatted_text(f"unknown command: {command}; try :help")
                continue
            value = await repl.line(line)
            if value is not None:
                print_formatted_text(json.dumps(await value.force_json(), indent=2, sort_keys=True))
        except (NixError, ReplRunError) as exc:
            _print_error(exc)


def _print_error(exc: NixError | ReplRunError) -> None:
    """Print a REPL error, preserving Nix's ANSI diagnostics."""
    if isinstance(exc, NixError):
        print_formatted_text(ANSI(exc.msg))
    else:
        print_formatted_text(f"error: {exc}")


async def _load_initial_target(repl: ReplSession, target: EvaluationTarget) -> list[str]:
    """Load a command-line target into the persistent REPL scope."""
    if target.file is None and target.flake is None:
        return []
    value = await load_repl_target(target, repl)
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
        process = await asyncio.create_subprocess_exec(str(program))
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
        process = await asyncio.create_subprocess_exec(*argv)
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
        process = await asyncio.create_subprocess_exec(*argv)
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
        process = await asyncio.create_subprocess_shell(command)
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
            main_program = await meta.attr("mainProgram").force_json()
            if not isinstance(main_program, str):
                raise ReplRunError("derivation meta.mainProgram is not a string")
            return main_program, None

    if await value.has_attr("pname"):
        pname = await value.attr("pname").force_json()
        if not isinstance(pname, str):
            raise ReplRunError("derivation pname is not a string")
        return pname, f"derivation has no meta.mainProgram; using pname {pname!r}"

    if not await value.has_attr("name"):
        raise ReplRunError("derivation has neither meta.mainProgram, pname, nor name")
    name = await value.attr("name").force_json()
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


class Repl(Command):
    """Open an interactive Nix evaluation session."""

    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")
    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()

    @override
    async def run(self) -> None:
        prepare_sys_path()

        target = EvaluationTarget.from_command(self)
        try:
            target.validate()
        except EvaluationTargetError as exc:
            print_formatted_text(f"error: {exc}")
            raise SystemExit(1) from exc

        async with nanopynix.Session(experimental_features=["flakes", "nix-command"]) as nix:
            with patch_stdout():
                async with forward_nix_logs(nix, log_file=sys.stdout), nix.store(self.store) as store, nix.repl(store) as repl:
                    prompt: PromptSession[str] = PromptSession(
                        completer=_ReplCompleter(repl),
                        complete_while_typing=True,
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
                        initial_sources.append((":load", str(target.file)))
                    elif target.flake is not None:
                        initial_sources.append((":load-flake", target.flake))
                    await _run_repl_loop(
                        repl,
                        prompt,
                        initial_loaded=initial_loaded,
                        initial_sources=initial_sources,
                        line_editors=repl.line_editors,
                    )
