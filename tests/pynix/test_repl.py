"""Tests for the interactive pynix REPL loop."""

from __future__ import annotations

from collections import deque
from typing import Any

from pynix import Pynix
from pynix.repl import Repl, _HELP, _run_repl_loop


class _Prompt:
    def __init__(self, lines: list[str]) -> None:
        self._lines = deque(lines)

    async def prompt_async(self, _prompt: str) -> str:
        return self._lines.popleft()


class _Value:
    async def force_json(self) -> object:
        return {"answer": 42}


class _Repl:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.loaded_files: list[str] = []

    async def line(self, text: str) -> _Value | None:
        self.lines.append(text)
        return None if text == "answer = 42" else _Value()

    async def load_file(self, path: str) -> _Value:
        self.loaded_files.append(path)
        return _Value()

    async def add_attrs(self, _value: _Value) -> list[str]:
        return ["answer"]


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


def test_repl_is_a_pynix_subcommand() -> None:
    assert isinstance(Pynix.parse(["repl"]).subcommand, Repl)
