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
import tempfile
from pathlib import Path

#: The variable that makes `pynix` record why a completion answered nothing.
#: It is the name `pynix._attr_completion` reads, and this module states it
#: rather than importing it: the gate drives the *installed* program, and the
#: suite has no `pynix` of its own to import.
DEBUG_VARIABLE = "PYNIX_COMPLETION_DEBUG"

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

    **A program that failed says so, and does not answer an empty set.** That
    is the same trap one step further in. `pynix` catches every failure of a
    completion on purpose, because a traceback drawn into a command line is
    worse than a missing candidate, so it exits 0 and offers nothing. This
    turns on the record that `pynix._attr_completion.DEBUG_VARIABLE` names,
    and reads it back: an empty answer with a recorded failure is a completer
    that never ran, and it is never the intended answer.

    Measured on `checks.completions`: 22 rows read `assert set() == {...}`
    and named no cause, twice, at 5 m 47 s a build. Issue #264.

    **Both programs get :data:`NIX_CONFIG`, and only one of them used to.**
    The baseline was configured and the program under test was not, so every
    row compared a `nix` that could read a flake against a `pynix` that could
    not. What the record then held was
    `experimental Nix feature 'flakes' is disabled`, from `lock_flake`. A gate
    that hands the two programs different configurations is not comparing
    them.
    """
    with tempfile.TemporaryDirectory() as room:
        record = Path(room) / "completion-failure.txt"
        answer = _driven(line, bin_dir, record)
        recorded = record.read_text(encoding="utf-8") if record.is_file() else ""
    if not answer and recorded:
        raise AssertionError(f"the completion of {line!r} failed and answered nothing:\n{recorded[:4000]}")
    return answer


def _driven(line: str, bin_dir: str, record: Path) -> set[str]:
    """Drive one completion, and write any failure of it to *record*."""
    program = shlex.split(line)[0]
    environment = {
        **nix_environment(),
        DEBUG_VARIABLE: str(record),
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
    if completed.returncode != 0:
        raise AssertionError(
            f"{program} exited {completed.returncode} while completing {line!r}:\n{completed.stderr[:4000]}"
        )
    # argcomplete puts a trailing space on a candidate it considers finished,
    # which is a hint to the shell and not part of the word.
    return {candidate.rstrip() for candidate in completed.stdout.split("\013") if candidate}
