"""Ask `nix` and ask an argcomplete program the same completion question.

Two modules of this directory compare the answers -- `test_nix_equivalence.py`
for `--file`, `test_flake_equivalence.py` for `--flake` -- so the two probes
live here rather than in either of them.

**Not `conftest.py`.** A function is an import and not a fixture, and this
repository holds several `conftest.py` files: pyright resolves that name
against the wrong one and reports the import as unknown. A module with a name
of its own resolves.
"""

from __future__ import annotations

import os
import shlex
import subprocess

#: What `nix` needs turned on before it answers a completion at all.
#:
#: **Named here rather than inherited from the machine.** `nix-command` is what
#: `NIX_GET_COMPLETIONS` runs under, and `completeFlakeRef` returns at once when
#: `Xp::Flakes` is off. A developer usually has both in their own configuration
#: and a build sandbox has neither, so a baseline that read the ambient setting
#: would answer nothing in the gate -- and an empty baseline against an empty
#: answer passes on any behaviour at all.
#:
#: It goes in the environment and not on the command line, because
#: `NIX_GET_COMPLETIONS` counts the arguments after `nix` and an extra flag
#: would move the one being completed.
NIX_CONFIG = "extra-experimental-features = nix-command flakes"


def nix_environment() -> dict[str, str]:
    """The environment of this process, with :data:`NIX_CONFIG` added to it."""
    return {**os.environ, "NIX_CONFIG": NIX_CONFIG}


#: Where the attribute path sits in `nix build --file F <path>`.
#: `nix` counts the arguments after the program name, and this line is
#: `build`(1) `--file`(2) `F`(3) `<path>`(4).
NIX_COMPLETION_INDEX = 4


def nix_candidates(arguments: list[str], index: int = NIX_COMPLETION_INDEX) -> set[str]:
    """What `nix` offers, through its own completion protocol.

    `NIX_GET_COMPLETIONS=<n>` makes `nix` print the kind of completion on the
    first line and then one candidate for each line after it, each one
    optionally followed by a tab and a description.
    """
    completed = subprocess.run(  # noqa: S603 -- `nix` from PATH, with arguments this module wrote
        ["nix", *arguments],  # noqa: S607 -- `nix` comes from the environment the gate builds
        env={**nix_environment(), "NIX_GET_COMPLETIONS": str(index)},
        capture_output=True,
        text=True,
        check=False,
    )
    lines = completed.stdout.splitlines()
    if not lines:
        raise AssertionError(f"nix answered nothing: {completed.stderr[:400]}")
    # The first line is the kind (`normal`, `filenames`, `attrs`).
    return {line.split("\t", 1)[0] for line in lines[1:] if line}


def argcomplete_candidates(line: str, bin_dir: str) -> set[str]:
    """What an argcomplete program offers for *line*, driven as a shell does.

    **The answer comes back on file descriptor 8**, which is what the script
    argcomplete generates redirects. A pipe on stdout gets nothing: the
    program writes its candidates to 8 and its ordinary output to 1.

    **`bin_dir` goes on PATH, and it is not optional.** A gate run has no
    `pynix` on its own PATH: it hands the suite a store path through
    `PYNIX_INSTALLED_PREFIX`, and the program that answers has to be the
    program under test. Without it `bash` reports "command not found", the
    answer is empty, and an empty answer looks like a completer that offered
    nothing rather than one that never ran.
    """
    program = shlex.split(line)[0]
    environment = {
        **os.environ,
        "PATH": os.pathsep.join([bin_dir, os.environ["PATH"]]),
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\013",
        "_ARGCOMPLETE_SHELL": "bash",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
        "COMP_TYPE": "9",
        "_ARGCOMPLETE_COMP_WORDBREAKS": " \t\n\"'><=;|&(:",
    }
    completed = subprocess.run(  # noqa: S603 -- bash, with a command line this module wrote
        ["bash", "-c", f"exec {shlex.quote(program)} 8>&1 1>/dev/null"],  # noqa: S607 -- same environment as `nix` above
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    # argcomplete puts a trailing space on a candidate it considers finished,
    # which is a hint to the shell and not part of the word.
    return {candidate.rstrip() for candidate in completed.stdout.split("\013") if candidate}
