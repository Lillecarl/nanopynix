"""Write the shell completion scripts of a clypi program, without installing them.

``nix/mk-app.nix`` runs this against the venv of an application, and
``installShellCompletion`` puts what it writes under ``share/`` so that a shell
loads it when the package is in ``environment.systemPackages`` or
``home.packages``.

**clypi offers a flag and not a protocol on stdout.** ``installShellCompletion``
knows the click convention -- run ``env _PROG_COMPLETE=source_bash prog`` and
read stdout -- and clypi reads no such variable, so a program asked that way
prints its help screen and exits 0. Issue #105 measured the result: three files
holding an ANSI-coloured help screen, each at a path that its shell loads.

**``install()`` is not the entry point for a build, and ``script()`` is.**
``AutocompleteInstaller.install`` writes to ``$HOME`` for fish and zsh and to
``/etc/bash_completion.d`` for bash, which no build sandbox can write.
``script()`` returns the same text and writes nothing.

**This imports a private module of clypi, on purpose.**
``clypi._cli.autocomplete`` holds the three installer classes, and clypi
publishes no other way to reach the text. The failure mode is the good one: an
upgrade that moves them breaks this build with an ``ImportError``, rather than
quietly installing something wrong. ``checks.completions`` reads the result and
states what a correct file looks like for each shell.

The scripts are dynamic. Each one calls the program back with
``_CLYPI_CURRENT_ARGS`` holding what the user has typed, and the program lists
the candidates. That costs one start of the program for each keypress, which
issue #123 took from 1.75 s to 0.145 s.

**Each script names its own shell, and clypi's does not.** clypi answers a
completion through ``list_arguments``, which reads only the options and the
subcommands of the command -- the answer does not depend on the shell at all.
It reaches that method through ``get_installer``, which does
``Path(os.environ["SHELL"]).name`` and raises. Measured on the built program:

    $ env -u SHELL _CLYPI_CURRENT_ARGS="pynix bu" pynix
    KeyError: 'SHELL'
    $ env SHELL=/bin/dash _CLYPI_CURRENT_ARGS="pynix bu" pynix
    ValueError: Autocomplete is not supported for shell 'dash'

So a user whose login shell clypi does not know, or a completion invoked where
``SHELL`` is unset, gets a Python traceback in the terminal instead of
candidates. :func:`_name_the_shell` puts ``SHELL`` into the callback, which is
also the more correct answer: the shell that is completing is the one running
the script, and not whatever the user's login shell happens to be.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

from clypi._cli.autocomplete import BashInstaller, FishInstaller, ZshInstaller

#: What every one of clypi's three templates puts in front of the callback.
_CALLBACK = "env _CLYPI_CURRENT_ARGS="


def _name_the_shell(text: str, shell: str) -> str:
    """Make the callback tell the program which shell is asking."""
    if _CALLBACK not in text:
        raise SystemExit(
            f"the {shell} template of clypi no longer spells its callback {_CALLBACK!r}, "
            "so the SHELL correction below did not apply. Read this module's docstring, "
            "check what clypi emits now, and fix the correction rather than dropping it."
        )
    return text.replace(_CALLBACK, f"env SHELL={shell} _CLYPI_CURRENT_ARGS=")


def main() -> None:
    module_name, command_name, out_dir = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
    command = getattr(importlib.import_module(module_name), command_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    for installer in (BashInstaller, FishInstaller, ZshInstaller):
        text = _name_the_shell(installer(command).script(), installer.shell)
        (out_dir / installer.shell).write_text(text + "\n", encoding="utf-8")
        print(f"{installer.shell}: {len(text)} bytes")  # noqa: T201 -- the build log is the report


main()
