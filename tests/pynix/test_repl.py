"""Tests for the interactive pynix REPL loop."""

# pyright: reportPrivateUsage=false
# Tests intentionally access private symbols from pynix.repl

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from nanopynix_proto.nix.common import LogLevel
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from pynix.repl import (  # type: ignore[reportPrivateUsage] -- tests intentionally access private symbols
    _HELP,
    Repl,
    ReplRunError,
    _completion_target,
    _derivation_name_part,
    _edit,
    _editor_argv,
    _exec_argv,
    _load_initial_target,
    _main_program,
    _nix_input,
    _NixLexer,
    _primary_output,
    _print_error,
    _repl_history,
    _ReplCompleter,
    _run_derivation,
    _run_repl_loop,
    _shell,
)
from pynix.target import EvaluationTarget

from nanopynix.exceptions import NixError
from pynix import Pynix

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment


class _Prompt:
    def __init__(self, lines: list[str]) -> None:
        self._lines = deque(lines)

    async def prompt_async(self, _prompt: str) -> str:
        return self._lines.popleft()


class _EOFPrompt:
    """A prompt that hangs up immediately, as if stdin were closed."""

    async def prompt_async(self, _prompt: str) -> str:
        raise EOFError


class _InterruptOncePrompt:
    """A prompt whose first read is interrupted (e.g. by Ctrl-C), then behaves normally."""

    def __init__(self) -> None:
        self._interrupted = False

    async def prompt_async(self, _prompt: str) -> str:
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return ":quit"


def test_repl_history_uses_xdg_state_home(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    history = _repl_history()
    history.append_string("pkgs.hello")

    assert (tmp_path / "state" / "pynix" / "repl_history").is_file()
    assert list(_repl_history().load_history_strings()) == ["pkgs.hello"]


class _Value:
    async def force_json(self) -> object:
        return {"answer": 42}


class _RunValue:
    def __init__(self, attrs: dict[str, object], outputs: dict[str, str]) -> None:
        self._attrs = attrs
        self._outputs = outputs

    async def has_attr(self, name: str) -> bool:
        return name in self._attrs

    def attr(self, name: str) -> _RunValue:
        value = self._attrs[name]
        return value if isinstance(value, _RunValue) else _RunValue({"value": value}, {})

    async def force_json(self) -> object:
        return self._attrs["value"]

    async def build(self) -> dict[str, str]:
        return self._outputs


class _CommandValue:
    def __init__(self, *, argv: list[str] | None = None, command: str | None = None) -> None:
        self._argv = argv
        self._command = command

    async def realise_argv(self) -> list[str]:
        if self._argv is None:
            raise AssertionError("unexpected :exec value")
        return self._argv

    async def realise_string(self) -> str:
        if self._command is None:
            raise AssertionError("unexpected :shell value")
        return self._command


class _EditValue:
    async def edit_location(self) -> tuple[str, int]:
        return "/tmp/source.nix", 42


class _TypeStub:
    name = "INT"


class _TypedValue:
    async def get_type(self) -> _TypeStub:
        return _TypeStub()


class _Repl:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.loaded_files: list[str] = []
        self.loaded_flakes: list[str] = []
        self.file_cache_resets = 0
        self.value: _Value | _RunValue | _CommandValue | _EditValue | _TypedValue = _Value()
        self.verbosity = LogLevel.NOTICE
        self.raise_on_line: str | None = None

    async def line(self, text: str) -> _Value | None:
        if text == self.raise_on_line:
            raise NixError("ThrownError", "boom failed")
        self.lines.append(text)
        return None if text == "answer = 42" else _Value()

    async def load_file(self, path: str) -> _Value:
        self.loaded_files.append(path)
        return _Value()

    async def eval_flake(self, ref: str) -> _Value:
        self.loaded_flakes.append(ref)
        return _Value()

    async def add_attrs(self, _value: object) -> list[str]:
        return ["answer"]

    async def reset_file_cache(self) -> None:
        self.file_cache_resets += 1

    async def get_verbosity(self) -> LogLevel:
        return self.verbosity

    async def set_verbosity(self, verbosity: LogLevel) -> LogLevel:
        self.verbosity = verbosity
        return verbosity

    async def string(self, _expr: str) -> _Value | _RunValue | _CommandValue | _EditValue | _TypedValue:
        return self.value


class _CompletionValue:
    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.released = False

    async def attr_names(self) -> list[str]:
        return self._names

    async def release(self) -> None:
        self.released = True


class _CompletionRepl:
    def __init__(self) -> None:
        self.value = _CompletionValue(["hello", "hello-unfree", "world"])

    async def scope_names(self) -> list[str]:
        return ["answer", "builtins", "pkgs"]

    async def string(self, expression: str) -> _CompletionValue:
        if expression == "badexpr":
            raise NixError("EvalError", "badexpr is undefined")
        assert expression == "pkgs"
        return self.value


async def test_repl_loop_keeps_bindings_and_prints_expression_values(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt(["answer = 42", "{ inherit answer; }", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert repl.lines == ["answer = 42", "{ inherit answer; }"]
    assert output == [
        _HELP,
        '{\n  "answer": 42\n}',
    ]


async def test_repl_load_command_uses_repl_load_file(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":load .", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert repl.loaded_files == ["."]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loads_file_target_into_initial_scope(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    names = await _load_initial_target(repl, EvaluationTarget(file=Path("default.nix"), attr=None, flake=None))  # type: ignore[arg-type] -- narrow REPL fake

    assert names == ["answer"]
    assert repl.loaded_files == ["default.nix"]
    assert output == ["Added 1 variables: answer"]


async def test_repl_last_loaded_includes_initial_target(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":ll", ":quit"]), initial_loaded=["flake", "pkgs"])  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, "flake pkgs"]


async def test_repl_verbosity_shows_and_updates_nix_log_level(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":verbosity", ":verbosity debug", ":verbosity", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, "notice (2)", "debug (6)", "debug (6)"]


def test_repl_preserves_nix_error_ansi(monkeypatch: Any) -> None:
    output: list[object] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    error = NixError("UndefinedVarError", "\x1b[31;1merror:\x1b[0m undefined variable 'll'")
    _print_error(error)

    assert len(output) == 1
    assert isinstance(output[0], ANSI)
    assert output[0].value == error.msg


def test_editor_argv_only_includes_line_for_line_aware_editors(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "hx --health")
    assert _editor_argv("/tmp/source.nix", 42, ("hx",)) == ["hx", "--health", "+42", "/tmp/source.nix"]

    monkeypatch.setenv("EDITOR", "cat")
    assert _editor_argv("/tmp/source.nix", 42, ("hx",)) == ["cat", "/tmp/source.nix"]


async def test_repl_edit_reloads_initial_sources(monkeypatch: Any) -> None:
    output: list[str] = []
    edited: list[_EditValue] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    async def edit(value: _EditValue, line_editors: tuple[str, ...]) -> int:
        edited.append(value)
        assert line_editors == ("hx",)
        return 0

    monkeypatch.setattr("pynix.repl._edit", edit)
    repl = _Repl()
    value = _EditValue()
    repl.value = value

    await _run_repl_loop(
        repl,  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol
        _Prompt([":edit target", ":quit"]),
        initial_sources=[(":load", "default.nix")],
        line_editors=("hx",),
    )

    assert edited == [value]
    assert repl.file_cache_resets == 1
    assert repl.loaded_files == ["default.nix"]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_run_prefers_meta_main_program(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "actual-program"
    program.parent.mkdir(parents=True)
    marker = tmp_path / "ran"
    program.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    program.chmod(0o755)
    value = _RunValue({"meta": _RunValue({"mainProgram": "actual-program"}, {})}, {"out": str(out)})

    assert await _run_derivation(value) == 0
    assert marker.read_text() == "ran\n"
    assert output == []


async def test_repl_run_command_runs_evaluated_derivation(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "run-me"
    program.parent.mkdir(parents=True)
    marker = tmp_path / "ran"
    program.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    program.chmod(0o755)
    repl = _Repl()
    repl.value = _RunValue({"meta": _RunValue({"mainProgram": "run-me"}, {})}, {"out": str(out)})

    await _run_repl_loop(repl, _Prompt([":run package", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert marker.read_text() == "ran\n"
    assert output == [_HELP]


async def test_repl_exec_runs_realised_argv_list(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    marker = tmp_path / "exec-ran"
    repl = _Repl()
    repl.value = _CommandValue(argv=["/bin/sh", "-c", f"echo exec > {marker}"])

    await _run_repl_loop(repl, _Prompt([":exec [ command ]", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert marker.read_text() == "exec\n"
    assert output == [_HELP]


async def test_repl_shell_runs_realised_string(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    marker = tmp_path / "shell-ran"
    repl = _Repl()
    repl.value = _CommandValue(command=f"echo shell > {marker}")

    await _run_repl_loop(repl, _Prompt([":shell command", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert marker.read_text() == "shell\n"
    assert output == [_HELP]


async def test_repl_run_warns_when_using_pname(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    value = _RunValue({"pname": "fallback-program"}, {})

    assert await _main_program(value) == (
        "fallback-program",
        "derivation has no meta.mainProgram; using pname 'fallback-program'",
    )
    assert _derivation_name_part("apache-httpd-2.0.48") == "apache-httpd"


async def test_repl_completion_uses_commands_scope_and_attrsets() -> None:
    completer = _ReplCompleter(_CompletionRepl())  # type: ignore[arg-type] -- narrow completion fake

    async def complete(text: str) -> list[str]:
        document = Document(text, cursor_position=len(text))
        return [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert await complete(":lo") == [":load", ":load-flake"]
    assert await complete("ans") == ["answer"]
    assert await complete(':shell "${') == ["answer", "builtins", "pkgs"]
    assert await complete("pkgs.") == ["hello", "hello-unfree", "world"]
    assert await complete("pkgs.hel") == ["hello", "hello-unfree"]
    assert await complete(':shell "${pk') == ["pkgs"]
    assert await complete(':shell "${pkgs.hel') == ["hello", "hello-unfree"]
    assert await complete(':shell "${pkgs.') == ["hello", "hello-unfree", "world"]
    assert completer._repl.value.released  # type: ignore[reportPrivateUsage] -- verifies temporary value lifetime


async def test_repl_completer_sync_hook_returns_no_completions() -> None:
    """prompt_toolkit calls the sync hook first; it must yield nothing so the async hook takes over."""
    completer = _ReplCompleter(_CompletionRepl())  # type: ignore[arg-type] -- narrow completion fake
    document = Document("pkgs.")

    assert list(completer.get_completions(document, CompleteEvent())) == []


async def test_repl_completer_returns_nothing_for_a_non_expression_command() -> None:
    completer = _ReplCompleter(_CompletionRepl())  # type: ignore[arg-type] -- narrow completion fake
    document = Document(":quit ", cursor_position=len(":quit "))

    completions = [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert completions == []


async def test_repl_completer_returns_nothing_when_cursor_has_no_completable_target() -> None:
    completer = _ReplCompleter(_CompletionRepl())  # type: ignore[arg-type] -- narrow completion fake
    document = Document("1 + 1 ", cursor_position=len("1 + 1 "))

    completions = [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert completions == []


async def test_repl_completer_swallows_nix_errors_from_attr_lookup() -> None:
    """A NixError while resolving attrs for completion must not blow up the prompt."""
    completer = _ReplCompleter(_CompletionRepl())  # type: ignore[arg-type] -- narrow completion fake
    document = Document("badexpr.", cursor_position=len("badexpr."))

    completions = [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert completions == []


def test_repl_tree_sitter_lexer_highlights_nix_expression() -> None:
    document = Document(':shell let x = 1; in "${x}"')

    fragments = _NixLexer().lex_document(document)(0)

    assert ("class:nix.keyword", "let") in fragments
    assert ("class:nix.number", "1") in fragments
    assert ("class:nix.punctuation", "${") in fragments
    assert ("class:nix.string", '"') in fragments


def test_repl_tree_sitter_lexer_skips_highlighting_for_non_expression_input() -> None:
    """A bare, argument-less command (e.g. ":quit") has no Nix source to highlight."""
    document = Document(":quit")

    fragments = _NixLexer().lex_document(document)(0)

    assert fragments == [("", ":quit")]


def test_repl_is_a_pynix_subcommand() -> None:
    assert isinstance(Pynix.parse(["repl"]).subcommand, Repl)


def test_repl_accepts_file_and_attr_options() -> None:
    command = Pynix.parse(["repl", "--file", "default.nix", "--attr", "pkgs"])

    assert isinstance(command.subcommand, Repl)
    assert command.subcommand.file == Path("default.nix")
    assert command.subcommand.attr == "pkgs"


# ── _nix_input: dumb coverage tests ─────────────────────────────────────
# _NixLexer and _ReplCompleter only ever feed _nix_input full, well-formed
# REPL lines, so the integration-style tests above never reach its
# argument-less-command and unrecognized-command branches. These pin each
# branch down directly.


def test_nix_input_plain_expression_has_zero_offset() -> None:
    assert _nix_input("1 + 1") == ("1 + 1", 0)


def test_nix_input_returns_none_for_command_without_argument() -> None:
    assert _nix_input(":build") is None


def test_nix_input_returns_none_for_non_expression_command() -> None:
    assert _nix_input(":quit extra") is None


def test_nix_input_returns_argument_and_offset_for_expression_command() -> None:
    assert _nix_input(":print 1 + 1") == ("1 + 1", len(":print "))


# ── _completion_target: dumb coverage tests ─────────────────────────────
# The existing completion test above only ever exercises inputs that
# resolve to a real completion. These pin down the "no completion here"
# branches: a dynamic/interpolated attrpath (no plain identifier to
# complete) and inputs with no completable node at the cursor at all.


def test_completion_target_returns_none_for_dynamic_attrpath() -> None:
    assert _completion_target("pkgs.${x}") is None


def test_completion_target_returns_none_when_nothing_matches() -> None:
    assert _completion_target(".") is None


# ── _run_repl_loop: prompt lifecycle and command dispatch ───────────────


async def test_repl_loop_returns_quietly_on_eof(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _EOFPrompt())  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, ""]


async def test_repl_loop_recovers_from_keyboard_interrupt(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _InterruptOncePrompt())  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, "^C"]


async def test_repl_loop_skips_blank_lines(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt(["   ", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert repl.lines == []
    assert output == [_HELP]


async def test_repl_loop_help_command_reprints_help(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":help", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, _HELP]


async def test_repl_loop_unknown_command_reports_error(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":bogus", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, "unknown command: :bogus; try :help"]


async def test_repl_loop_print_command_evaluates_expression(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":print 1 + 1", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, '{\n  "answer": 42\n}']


async def test_repl_loop_type_command_shows_nix_type(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _TypedValue()

    await _run_repl_loop(repl, _Prompt([":type 1", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, "int"]


async def test_repl_loop_build_command_prints_outputs(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _RunValue({}, {"out": "/nix/store/xxx-out"})

    await _run_repl_loop(repl, _Prompt([":build pkgs.hello", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, '{\n  "out": "/nix/store/xxx-out"\n}']


async def test_repl_loop_add_command_adds_attrs(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":add pkgs", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loop_load_flake_command_uses_repl_eval_flake(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":load-flake .#hello", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert repl.loaded_flakes == [".#hello"]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loop_reload_command_reruns_loaded_sources(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":reload", ":quit"]), initial_sources=[(":load", "default.nix")])  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert repl.file_cache_resets == 1
    assert repl.loaded_files == ["default.nix"]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loop_verbosity_reports_invalid_level(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":verbosity bogus-level", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert len(output) == 2
    assert output[0] == _HELP
    assert output[1].startswith("error: unknown verbosity 'bogus-level'")


async def test_repl_loop_reports_nix_error_from_a_line_expression(monkeypatch: Any) -> None:
    """The loop's except clause must also fire for plain-expression evaluation, not just commands."""
    output: list[object] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.raise_on_line = "boom"

    await _run_repl_loop(repl, _Prompt(["boom", ":quit"]))  # type: ignore[arg-type] -- _Repl is a test double matching ReplSession protocol

    assert len(output) == 2
    assert isinstance(output[1], ANSI)


def test_print_error_formats_repl_run_error_as_plain_text(monkeypatch: Any) -> None:
    """Unlike NixError, a ReplRunError has no ANSI diagnostics to preserve."""
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    _print_error(ReplRunError("boom"))

    assert output == ["error: boom"]


async def test_load_initial_target_returns_empty_when_no_file_or_flake_given() -> None:
    names = await _load_initial_target(_Repl(), EvaluationTarget(file=None, attr=None, flake=None))  # type: ignore[arg-type] -- narrow REPL fake

    assert names == []


# ── _run_derivation: build/run success, warnings, and failures ──────────


async def test_run_derivation_prints_main_program_fallback_warning(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "fallback-program"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\ntrue\n")
    program.chmod(0o755)
    value = _RunValue({"pname": "fallback-program"}, {"out": str(out)})

    assert await _run_derivation(value) == 0
    assert output == ["warning: derivation has no meta.mainProgram; using pname 'fallback-program'"]


async def test_run_derivation_reports_exec_failure(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    value = _RunValue({"meta": _RunValue({"mainProgram": "missing-program"}, {})}, {"out": str(out)})

    with pytest.raises(ReplRunError, match="cannot run"):
        await _run_derivation(value)


async def test_run_derivation_warns_on_nonzero_exit(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "failing"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\nexit 1\n")
    program.chmod(0o755)
    value = _RunValue({"meta": _RunValue({"mainProgram": "failing"}, {})}, {"out": str(out)})

    assert await _run_derivation(value) == 1
    assert output == [f"warning: {program} exited with status 1"]


# ── _editor_argv / _edit: editor launch edge cases ───────────────────────


def test_editor_argv_rejects_unparsable_editor(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", 'hx "unterminated')

    with pytest.raises(ReplRunError, match=r"cannot parse \$EDITOR"):
        _editor_argv("/tmp/source.nix", 1, ())


def test_editor_argv_rejects_empty_editor(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "   ")

    with pytest.raises(ReplRunError, match=r"\$EDITOR is empty"):
        _editor_argv("/tmp/source.nix", 1, ())


async def test_edit_runs_editor_and_returns_code(tmp_path: Path, monkeypatch: Any) -> None:
    marker = tmp_path / "edited"
    editor = tmp_path / "editor.sh"
    editor.write_text(f"#!/bin/sh\necho edited > {marker}\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))

    assert await _edit(_EditValue(), ()) == 0
    assert marker.read_text() == "edited\n"


async def test_edit_reports_launch_failure(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "/nonexistent/editor-binary")

    with pytest.raises(ReplRunError, match="cannot start editor"):
        await _edit(_EditValue(), ())


async def test_edit_warns_on_nonzero_exit(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    editor = tmp_path / "editor.sh"
    editor.write_text("#!/bin/sh\nexit 3\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))

    assert await _edit(_EditValue(), ()) == 3
    assert output == ["warning: editor exited with status 3"]


# ── _exec_argv / _shell / _primary_output: dumb coverage tests ──────────
# These are only reached today through the ":exec"/":shell"/":build" REPL
# commands with hand-picked happy-path fixtures elsewhere in this file, so
# their empty-input, launch-failure, and nonzero-exit branches are
# otherwise untested.


async def test_exec_argv_rejects_empty_list() -> None:
    with pytest.raises(ReplRunError, match="command list is empty"):
        await _exec_argv([])


async def test_exec_argv_reports_launch_failure() -> None:
    with pytest.raises(ReplRunError, match="cannot run"):
        await _exec_argv(["/nonexistent/binary-xyz"])


async def test_exec_argv_warns_on_nonzero_exit(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    return_code = await _exec_argv(["/bin/sh", "-c", "exit 7"])

    assert return_code == 7
    assert output == ["warning: /bin/sh exited with status 7"]


def test_primary_output_rejects_missing_out() -> None:
    with pytest.raises(ReplRunError, match="no 'out' output"):
        _primary_output({"lib": "/nix/store/xxx"})


async def test_shell_rejects_empty_command() -> None:
    with pytest.raises(ReplRunError, match="command is empty"):
        await _shell("")


async def test_shell_reports_launch_failure(monkeypatch: Any) -> None:
    async def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _raise)

    with pytest.raises(ReplRunError, match="cannot start shell"):
        await _shell("echo hi")


async def test_shell_warns_on_nonzero_exit(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)

    return_code = await _shell("exit 5")

    assert return_code == 5
    assert output == ["warning: shell exited with status 5"]


# ── _main_program / _derivation_name_part: remaining branches ───────────


async def test_main_program_falls_back_to_pname_when_meta_lacks_main_program() -> None:
    value = _RunValue({"meta": _RunValue({}, {}), "pname": "fallback"}, {})

    assert await _main_program(value) == (
        "fallback",
        "derivation has no meta.mainProgram; using pname 'fallback'",
    )


async def test_main_program_rejects_non_string_meta_main_program() -> None:
    value = _RunValue({"meta": _RunValue({"mainProgram": 123}, {})}, {})

    with pytest.raises(ReplRunError, match="meta.mainProgram is not a string"):
        await _main_program(value)


async def test_main_program_rejects_non_string_pname() -> None:
    value = _RunValue({"pname": 123}, {})

    with pytest.raises(ReplRunError, match="pname is not a string"):
        await _main_program(value)


async def test_main_program_rejects_derivation_with_no_identifying_attrs() -> None:
    value = _RunValue({}, {})

    with pytest.raises(ReplRunError, match="has neither meta.mainProgram, pname, nor name"):
        await _main_program(value)


async def test_main_program_rejects_non_string_name() -> None:
    value = _RunValue({"name": 123}, {})

    with pytest.raises(ReplRunError, match="name is not a string"):
        await _main_program(value)


async def test_main_program_falls_back_to_derivation_name() -> None:
    value = _RunValue({"name": "hello-1.2.3"}, {})

    assert await _main_program(value) == (
        "hello",
        "derivation has no meta.mainProgram; using name 'hello'",
    )


def test_derivation_name_part_keeps_dashless_names_whole() -> None:
    assert _derivation_name_part("hello") == "hello"


# ── Repl.run(): full interactive session, end to end ─────────────────────


class _ScriptedPromptSession:
    """Stand-in for prompt_toolkit's PromptSession, feeding scripted lines then EOF."""

    def __init__(self, lines: list[str], **_kwargs: object) -> None:
        self._lines = deque(lines)

    async def prompt_async(self, _prompt: str) -> str:
        if not self._lines:
            raise EOFError
        return self._lines.popleft()


async def test_repl_run_executes_a_full_interactive_session(
    shared_nix_environment: NixTestEnvironment, monkeypatch: Any
) -> None:
    """Exercises Repl.run() itself against a real store/session with a scripted prompt.

    Everything else in this file drives _run_repl_loop directly with fakes;
    nothing else builds and runs the real command end to end, so Repl.run()
    (PromptSession/store/repl-session wiring) has no coverage otherwise.
    """
    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession(["1 + 1", ":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix.repl.PromptSession", _fake_prompt_session)

    cmd = Pynix.parse(["repl", *shared_nix_environment.pynix_store_args()])
    await cmd.astart()

    assert output[0] == _HELP
    assert "2" in output[1]


async def test_repl_run_rejects_mutually_exclusive_file_and_flake(tmp_path: Path, monkeypatch: Any) -> None:
    """Exercises Repl.run()'s own target.validate() error handling before any store is opened."""
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{}")

    cmd = Pynix.parse(["repl", "--file", str(nix_file), "--flake", ".#hello"])

    with pytest.raises(SystemExit):
        await cmd.astart()
    assert output == ["error: --file and --flake are mutually exclusive"]


async def test_repl_run_loads_a_file_target_into_initial_scope(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, monkeypatch: Any
) -> None:
    """Covers Repl.run()'s --file initial_sources branch, not exercised by the plain-session test above."""

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession([":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix.repl.PromptSession", _fake_prompt_session)
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ answer = 42; }")

    cmd = Pynix.parse(["repl", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()

    assert output == ["Added 1 variables: answer", _HELP]


async def test_repl_run_loads_a_flake_target_into_initial_scope(
    shared_nix_environment: NixTestEnvironment, git_flake: Path, monkeypatch: Any
) -> None:
    """Covers Repl.run()'s --flake initial_sources branch (the --file variant is covered above)."""

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession([":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix.repl.PromptSession", _fake_prompt_session)

    cmd = Pynix.parse(["repl", "--flake", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()

    assert output[0].startswith("Added ")
    assert "hello" in output[0].split()
    assert "greeting" in output[0].split()
    assert output[1] == _HELP


async def test_repl_run_reports_missing_attr_in_initial_target(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, monkeypatch: Any
) -> None:
    """Covers Repl.run()'s EvaluationTargetError handling around _load_initial_target."""

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession([":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix.repl.PromptSession", _fake_prompt_session)
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ answer = 42; }")

    cmd = Pynix.parse(
        ["repl", "--file", str(nix_file), "--attr", "missing", *shared_nix_environment.pynix_store_args()]
    )

    with pytest.raises(SystemExit):
        await cmd.astart()
    assert len(output) == 1
    assert output[0].startswith("error: attribute 'missing' not found")
