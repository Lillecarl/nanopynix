"""Tests for the interactive pynix REPL loop."""

# pyright: reportPrivateUsage=false
# Tests intentionally access private symbols from pynix.repl

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from nanopynix_proto.nix.common import LogLevel
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from pynix.repl import (  # type: ignore[reportPrivateUsage] -- tests intentionally access private symbols
    _HELP,
    Repl,
    _derivation_name_part,
    _editor_argv,
    _load_initial_target,
    _main_program,
    _print_error,
    _ReplCompleter,
    _run_derivation,
    _run_repl_loop,
)
from pynix.target import EvaluationTarget

from nanopynix.exceptions import NixError
from pynix import Pynix


class _Prompt:
    def __init__(self, lines: list[str]) -> None:
        self._lines = deque(lines)

    async def prompt_async(self, _prompt: str) -> str:
        return self._lines.popleft()


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


class _Repl:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.loaded_files: list[str] = []
        self.file_cache_resets = 0
        self.value: _Value | _RunValue | _CommandValue | _EditValue = _Value()
        self.verbosity = LogLevel.NOTICE

    async def line(self, text: str) -> _Value | None:
        self.lines.append(text)
        return None if text == "answer = 42" else _Value()

    async def load_file(self, path: str) -> _Value:
        self.loaded_files.append(path)
        return _Value()

    async def add_attrs(self, _value: _Value) -> list[str]:
        return ["answer"]

    async def reset_file_cache(self) -> None:
        self.file_cache_resets += 1

    async def get_verbosity(self) -> LogLevel:
        return self.verbosity

    async def set_verbosity(self, verbosity: LogLevel) -> LogLevel:
        self.verbosity = verbosity
        return verbosity

    async def string(self, _expr: str) -> _Value | _RunValue | _CommandValue | _EditValue:
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
    assert await complete("pkgs.") == ["hello", "hello-unfree", "world"]
    assert await complete("pkgs.hel") == ["hello", "hello-unfree"]
    assert completer._repl.value.released  # type: ignore[reportPrivateUsage] -- verifies temporary value lifetime


def test_repl_is_a_pynix_subcommand() -> None:
    assert isinstance(Pynix.parse(["repl"]).subcommand, Repl)


def test_repl_accepts_file_and_attr_options() -> None:
    command = Pynix.parse(["repl", "--file", "default.nix", "--attr", "pkgs"])

    assert isinstance(command.subcommand, Repl)
    assert command.subcommand.file == Path("default.nix")
    assert command.subcommand.attr == "pkgs"
