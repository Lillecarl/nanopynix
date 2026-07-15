"""Tests for the interactive pynix REPL loop."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from pynix import Pynix
from pynix.repl import (
    Repl,
    _HELP,
    _ReplCompleter,
    _derivation_name_part,
    _main_program,
    _run_derivation,
    _run_repl_loop,
)


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


class _Repl:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.loaded_files: list[str] = []
        self.value: _Value | _RunValue | _CommandValue = _Value()

    async def line(self, text: str) -> _Value | None:
        self.lines.append(text)
        return None if text == "answer = 42" else _Value()

    async def load_file(self, path: str) -> _Value:
        self.loaded_files.append(path)
        return _Value()

    async def add_attrs(self, _value: _Value) -> list[str]:
        return ["answer"]

    async def string(self, _expr: str) -> _Value | _RunValue | _CommandValue:
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

    await _run_repl_loop(repl, _Prompt(["answer = 42", "{ inherit answer; }", ":quit"]))

    assert repl.lines == ["answer = 42", "{ inherit answer; }"]
    assert output == [
        _HELP,
        '{\n  "answer": 42\n}',
    ]


async def test_repl_load_command_uses_repl_load_file(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":load .", ":quit"]))

    assert repl.loaded_files == ["."]
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

    await _run_repl_loop(repl, _Prompt([":run package", ":quit"]))

    assert marker.read_text() == "ran\n"
    assert output == [_HELP]


async def test_repl_exec_runs_realised_argv_list(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    marker = tmp_path / "exec-ran"
    repl = _Repl()
    repl.value = _CommandValue(argv=["/bin/sh", "-c", f"echo exec > {marker}"])

    await _run_repl_loop(repl, _Prompt([":exec [ command ]", ":quit"]))

    assert marker.read_text() == "exec\n"
    assert output == [_HELP]


async def test_repl_shell_runs_realised_string(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix.repl.print_formatted_text", output.append)
    marker = tmp_path / "shell-ran"
    repl = _Repl()
    repl.value = _CommandValue(command=f"echo shell > {marker}")

    await _run_repl_loop(repl, _Prompt([":shell command", ":quit"]))

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
    assert await complete("pkgs.hel") == ["hello", "hello-unfree"]
    assert completer._repl.value.released  # type: ignore[reportPrivateUsage] -- verifies temporary value lifetime


def test_repl_is_a_pynix_subcommand() -> None:
    assert isinstance(Pynix.parse(["repl"]).subcommand, Repl)
