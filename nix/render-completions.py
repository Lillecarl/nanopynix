"""Write the shell completion scripts of an argcomplete program.

``nix/mk-app.nix`` runs this against the venv of an application, and
``installShellCompletion`` puts what it writes under ``share/`` so that a shell
loads it when the package is in ``environment.systemPackages`` or
``home.packages``.

**The whole script comes from argcomplete, through public API.**
``argcomplete.shell_integration.shellcode`` is what ``register-python-argcomplete``
calls, and it takes the name of the program and the name of the shell. Nothing
here knows what the completion protocol is.

This file used to be four times as long. clypi published no way to reach its
completion script at all, so the renderer imported ``clypi._cli.autocomplete``,
called a method that ``install()`` was supposed to call, and then rewrote the
callback of every generated script because clypi resolved a completion through
``os.environ["SHELL"]`` and raised when that variable named a shell it did not
know. Issue #214 replaced clypi with argparse and argcomplete, and every one of
those workarounds went with it.
"""

from __future__ import annotations

import pathlib
import sys

# pyright: reportUnknownVariableType=false
# argcomplete ships no type information and nixpkgs carries no stubs for it, so
# `shellcode` is a partially unknown function. Scoped to this file, which is
# eleven lines of build glue and imports nothing else.
from argcomplete.shell_integration import shellcode

#: The shells this writes a script for. `installShellCompletion` in
#: nix/mk-app.nix installs one file for each.
SHELLS = ("bash", "zsh", "fish")


def main() -> None:
    program, out_dir = sys.argv[1], pathlib.Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    for shell in SHELLS:
        text = shellcode([program], shell=shell)
        (out_dir / shell).write_text(text, encoding="utf-8")
        print(f"{shell}: {len(text)} bytes")  # noqa: T201 -- the build log is the report


main()
