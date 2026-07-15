"""Interactive Nix REPL command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, override

from clypi import Command, arg
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text

from nanopynix.exceptions import NixError
from pynix._util import forward_nix_logs, prepare_sys_path

if TYPE_CHECKING:
    from nanopynix import ReplSession

_DEFAULT_STORE = "auto"
_PROMPT = "pynix> "
_HELP = """Commands:
  <expr>                 Evaluate and print an expression
  <name> = <expr>        Bind an expression to a name
  :a, :add <expr>        Add an attrset's attributes to scope
  :b, :build <expr>      Build an evaluated derivation
  :l, :load <path>       Load a Nix file and add its attributes to scope
  :lf, :load-flake <ref> Load flake outputs into scope
  :ll, :last-loaded      Show names from the most recent load
  :p, :print <expr>      Evaluate and print an expression
  :r, :reload            Reload files and flakes
  :t, :type <expr>       Show an expression's Nix type
  :q, :quit              Exit the REPL
  :?, :help              Show this help"""


async def _run_repl_loop(repl: ReplSession, prompt: Any) -> None:
    """Read and evaluate lines until the user exits the REPL."""
    loaded: list[tuple[str, str]] = []
    last_loaded: list[str] = []

    async def add_attrs(value: Any) -> list[str]:
        nonlocal last_loaded
        last_loaded = await repl.add_attrs(value)
        print_formatted_text(f"Added {len(last_loaded)} variables: {' '.join(last_loaded)}")
        return last_loaded

    async def evaluate(expr: str) -> Any:
        value = await repl.string(expr)
        print_formatted_text(json.dumps(await value.force_json(), indent=2, sort_keys=True))
        return value

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
                for load_command, source in loaded:
                    value = await (repl.load_file(source) if load_command == ":load" else repl.eval_flake(source))
                    await add_attrs(value)
                continue
            if command.startswith(":"):
                print_formatted_text(f"unknown command: {command}; try :help")
                continue
            value = await repl.line(line)
            if value is not None:
                print_formatted_text(json.dumps(await value.force_json(), indent=2, sort_keys=True))
        except NixError as exc:
            print_formatted_text(f"error: {exc}")


class Repl(Command):
    """Open an interactive Nix evaluation session."""

    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        prepare_sys_path()
        import nanopynix

        prompt = PromptSession()
        async with (
            nanopynix.Session() as nix,
            forward_nix_logs(nix),
            nix.store(self.store) as store,
            nix.repl(store) as repl,
        ):
            with patch_stdout():
                await _run_repl_loop(repl, prompt)
