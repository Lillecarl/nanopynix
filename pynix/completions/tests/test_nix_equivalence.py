"""What `pynix` offers for an attribute path, against what `nix` offers.

**The baseline is a program, and not a table this repository wrote.** `nix`
answers its own completion through `NIX_GET_COMPLETIONS`, so "the same as Nix"
is a thing a test can ask rather than a thing a reader has to believe. When Nix
changes its answer, this fails and names the difference.

Three spellings mean one selection, and all three have to agree::

    nix   build --file F a.b
    pynix build --file F --attr a.b
    pynix build --file F#a.b

`pynix.target.EvaluationTarget.selected_attr` already joins a `#` fragment and
`--attr` at run time, so a completion that answered them differently would
contradict what the command then does.

**The `#` on `--file` is ours, and the rest is Nix's.** `nix` has no `--file
F#attr` spelling: it takes the attribute path as a positional argument. So this
module asserts equivalence of the *candidate set* and not of the syntax that
carries it, and it strips the `F#` that our spelling puts in front of each
candidate.

**No value is forced, and the fixture proves it.** `throws` below is
`builtins.derivation {}`, which raises when it is evaluated. Every prefix here
lists a set that holds it, so a completer that forced its values would fail
rather than answer.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

#: The Nix file both programs complete against.
#:
#: Small on purpose: a completion runs while a person holds a key down, and a
#: fixture that pulled nixpkgs in would measure the evaluation of nixpkgs.
EQUIVALENCE_SOURCE = """\
let
  throws = builtins.derivation { };
in
{
  nixos = {
    _type = "attrs";
    class = "nixos";
    config.system.build.toplevel = throws;
  };
  nixosLater = { marker = "second"; };
  other = { deep = { deeper = "leaf"; }; };
}
"""

#: The attribute prefixes to ask both programs about.
#:
#: Each one is a shape a caller types: nothing at all, an ambiguous stem, a
#: stem that matches one name, a trailing dot, and two levels of nesting.
PREFIXES = ("", "nixo", "nixosL", "nixos.", "nixos.config.", "nixos.config.system.", "other.deep.")

#: Where the attribute path sits in `nix build --file F <path>`.
#: `nix` counts the arguments after the program name, and this line is
#: `build`(1) `--file`(2) `F`(3) `<path>`(4).
NIX_COMPLETION_INDEX = 4


@pytest.fixture(scope="session")
def equivalence_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The Nix file that both programs are asked about."""
    path = tmp_path_factory.mktemp("equivalence") / "attrs.nix"
    path.write_text(EQUIVALENCE_SOURCE, encoding="utf-8")
    return path


def nix_candidates(arguments: list[str], index: int = NIX_COMPLETION_INDEX) -> set[str]:
    """What `nix` offers, through its own completion protocol.

    `NIX_GET_COMPLETIONS=<n>` makes `nix` print the kind of completion on the
    first line and then one candidate for each line after it, each one
    optionally followed by a tab and a description.
    """
    completed = subprocess.run(  # noqa: S603 -- `nix` from PATH, with arguments this module wrote
        ["nix", *arguments],  # noqa: S607 -- `nix` comes from the environment the gate builds
        env={**os.environ, "NIX_GET_COMPLETIONS": str(index)},
        capture_output=True,
        text=True,
        check=False,
    )
    lines = completed.stdout.splitlines()
    if not lines:
        raise AssertionError(f"nix answered nothing: {completed.stderr[:400]}")
    # The first line is the kind (`normal`, `filenames`, `attrs`).
    return {line.split("\t", 1)[0] for line in lines[1:] if line}


def argcomplete_candidates(line: str) -> set[str]:
    """What an argcomplete program offers for *line*, driven as a shell does.

    **The answer comes back on file descriptor 8**, which is what the script
    argcomplete generates redirects. A pipe on stdout gets nothing: the
    program writes its candidates to 8 and its ordinary output to 1.
    """
    program = shlex.split(line)[0]
    environment = {
        **os.environ,
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


@pytest.mark.parametrize("prefix", PREFIXES)
def test_attr_offers_what_nix_offers(prefix: str, equivalence_file: Path) -> None:
    """`--attr` answers the set that `nix` answers for the same file."""
    baseline = nix_candidates(["build", "--file", str(equivalence_file), prefix])
    ours = argcomplete_candidates(f"pynix build --file {equivalence_file} --attr {prefix}")
    assert ours == baseline


@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_hash_spelling_offers_what_nix_offers(prefix: str, equivalence_file: Path) -> None:
    """`--file F#a.b` answers the same set, with `F#` in front of each candidate.

    The spelling is ours and the answer is Nix's. `EvaluationTarget.selected_attr`
    joins the fragment and `--attr`, so the two spellings name one selection and
    a completion that split them would be a defect a caller meets at run time.
    """
    baseline = nix_candidates(["build", "--file", str(equivalence_file), prefix])
    ours = argcomplete_candidates(f"pynix build --file {equivalence_file}#{prefix}")
    head = f"{equivalence_file}#"
    assert all(candidate.startswith(head) for candidate in ours), ours
    assert {candidate.removeprefix(head) for candidate in ours} == baseline


def test_no_value_is_forced(equivalence_file: Path) -> None:
    """The control for every row above.

    `nixos.config.system.build.toplevel` is `builtins.derivation { }`, which
    throws when it is evaluated. Listing the names of the set that holds it
    must not touch it. Without this, a completer that forced every value would
    still pass each row that lists a set of plain strings.
    """
    ours = argcomplete_candidates(f"pynix build --file {equivalence_file} --attr nixos.config.system.build.")
    assert ours == {"nixos.config.system.build.toplevel"}
