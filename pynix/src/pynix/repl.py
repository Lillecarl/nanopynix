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
_HELP = "Enter Nix expressions or bindings. Commands: :help, :quit (or :q)."


async def _run_repl_loop(repl: ReplSession, prompt: Any) -> None:
    """Read and evaluate lines until the user exits the REPL."""
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
        if text in {":quit", ":q"}:
            return
        if text == ":help":
            print_formatted_text(_HELP)
            continue
        if text.startswith(":"):
            print_formatted_text(f"unknown command: {text}; try :help")
            continue

        try:
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
