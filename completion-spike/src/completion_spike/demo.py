"""A tiny cyclopts program, shaped like `pynix` and no larger.

Each part of the shape is here because it broke something before, or because
it is the part the spike has to answer:

- ``store gc print-roots`` is three levels deep, so the path gates of each
  generated script are exercised rather than assumed.
- ``--print-build-logs`` defaults to true, so it has a second long spelling
  (``--no-print-build-logs``). A hand-written fish renderer lost the first of
  the two spellings, because fish keeps only the last ``-l`` of one
  ``complete`` line.
- ``--attr`` is the option whose value the program completes at run time. It is
  the whole reason this package exists.

``_complete-value`` returns a fixed list. The subject here is the shell
plumbing, and not the evaluation of Nix, so nothing imports `nanopynix`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from completion_spike._layer import DynamicValue, Shell, render_script
from completion_spike._line import read_line

#: What ``--attr`` completes into, for each engine.
#:
#: **The two lists differ, and that is the point.** A real `--attr` is
#: evaluated against the target that the rest of the command line names, so a
#: completion that cannot read `--engine` would answer for the wrong store.
#: `pynix --engine local build --attr <TAB>` is the case this stands for.
CANDIDATES = {
    "local": (
        "hello",
        "hello.x86_64-linux",
        "python3",
        "python3Packages.rich",
    ),
    "remote": (
        "hello",
        "hello.aarch64-linux",
        "remote-only",
    ),
}

#: The engine that a command line names when it names none.
DEFAULT_ENGINE = "local"

#: Names a file that every start of this program appends one line to. The tests
#: use it to prove the split that this package is about: a static completion
#: must start no process, and a dynamic one must start exactly one.
CALL_LOG_VARIABLE = "COMPLETION_SPIKE_CALL_LOG"

#: The one option that completes dynamically, and the command that answers for
#: it. `_layer` turns this into shell code, and `_complete_value` below is the
#: other end of it.
DYNAMIC_VALUES = (DynamicValue(command_path=("build",), option="--attr", subcommand="_complete-value"),)

app = App(name="demo", help="A tiny program, to test shell completion with.")

store = App(name="store", help="Talk to a store.")
app.command(store)

gc = App(name="gc", help="Collect garbage.")
store.command(gc)


@app.command
def build(
    *,
    attr: Annotated[str, Parameter(help="The attribute to build.")] = "",
    engine: Annotated[str, Parameter(help="Which engine to build with.")] = DEFAULT_ENGINE,
    print_build_logs: Annotated[bool, Parameter(help="Print build log lines.")] = True,
) -> None:
    """Build a thing."""


@gc.command(name="print-roots")
def print_roots() -> None:
    """List the roots."""


@app.command(name="_complete-value", show=False)
def complete_value(*, line: str = "") -> None:
    """Write each candidate for ``--attr`` that the command line allows.

    *line* is the whole command line up to the cursor, and not only the word
    being completed. **A completion that reads one word cannot be correct
    here**: the attributes to offer depend on `--engine`, and in a real program
    they would depend on `--file` and `--flake` too. `read_line` takes both the
    prefix and the rest of the context out of it.

    One candidate for each line, which is what every shell here reads.
    """
    context = read_line(line)
    engine = context.option("--engine", DEFAULT_ENGINE)
    for candidate in CANDIDATES.get(engine, ()):
        if candidate.startswith(context.prefix):
            print(candidate)  # noqa: T201 -- the candidates are this command's output, read by a shell


@app.command(name="_completion", show=False)
def completion_script(shell: Shell) -> None:
    """Write the whole completion script for *shell*.

    The static half comes from cyclopts, and the dynamic half from `_layer`.
    They are one file because a shell loads one file, and because the layer has
    to be able to say that it rests on the static half rather than replacing
    it.
    """
    static = app.generate_completion(shell=shell)
    print(render_script(shell, "demo", DYNAMIC_VALUES, static))  # noqa: T201 -- the script is this command's output


def _record_call() -> None:
    """Append one line to the call log, when the tests asked for one."""
    path = os.environ.get(CALL_LOG_VARIABLE)
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as log:
        log.write(" ".join(sys.argv[1:]) + "\n")


def main() -> None:
    _record_call()
    app()
