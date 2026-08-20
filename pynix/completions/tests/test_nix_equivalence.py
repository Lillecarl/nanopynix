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


@pytest.fixture(scope="session")
def equivalence_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory holding the same source as its ``default.nix``.

    A directory is a shape of ``--file`` that a caller writes often, and it is
    the one that needs the autocall: ``nix build --file DIR attr`` reads
    ``DIR/default.nix``. A test that only ever named a file would not see a
    completer that forgot to.
    """
    directory = tmp_path_factory.mktemp("equivalence-directory")
    (directory / "default.nix").write_text(EQUIVALENCE_SOURCE, encoding="utf-8")
    return directory


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


@pytest.mark.parametrize("prefix", PREFIXES)
def test_attr_offers_what_nix_offers(prefix: str, equivalence_file: Path, pynix_bin: str) -> None:
    """`--attr` answers the set that `nix` answers for the same file."""
    baseline = nix_candidates(["build", "--file", str(equivalence_file), prefix])
    assert baseline, "nix offered nothing, so this row would pass on any answer at all"
    ours = argcomplete_candidates(f"pynix build --file {equivalence_file} --attr {prefix}", pynix_bin)
    assert ours == baseline


@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_hash_spelling_offers_what_nix_offers(prefix: str, equivalence_file: Path, pynix_bin: str) -> None:
    """`--file F#a.b` answers the same set, with `F#` in front of each candidate.

    The spelling is ours and the answer is Nix's. `EvaluationTarget.selected_attr`
    joins the fragment and `--attr`, so the two spellings name one selection and
    a completion that split them would be a defect a caller meets at run time.
    """
    baseline = nix_candidates(["build", "--file", str(equivalence_file), prefix])
    assert baseline, "nix offered nothing, so this row would pass on any answer at all"
    ours = argcomplete_candidates(f"pynix build --file {equivalence_file}#{prefix}", pynix_bin)
    head = f"{equivalence_file}#"
    assert all(candidate.startswith(head) for candidate in ours), ours
    assert {candidate.removeprefix(head) for candidate in ours} == baseline


def test_no_value_is_forced(equivalence_file: Path, pynix_bin: str) -> None:
    """The control for every row above.

    `nixos.config.system.build.toplevel` is `builtins.derivation { }`, which
    throws when it is evaluated. Listing the names of the set that holds it
    must not touch it. Without this, a completer that forced every value would
    still pass each row that lists a set of plain strings.
    """
    ours = argcomplete_candidates(f"pynix build --file {equivalence_file} --attr nixos.config.system.build.", pynix_bin)
    assert ours == {"nixos.config.system.build.toplevel"}


#: How a caller can spell the same target on the command line.
#:
#: **Every one of these is our own resolution, and every one has to agree with
#: Nix.** `pynix.target.resolve_file_reference` adds a fragment and a bare name
#: on top of the four shapes the evaluator resolves itself, so a spelling that
#: worked for `nix` and not for `pynix` would be ours to answer for.
SHAPES = ("absolute", "relative", "dot-relative", "directory")


def spell(shape: str, path: Path, directory: Path) -> str:
    """The command-line spelling of *shape*."""
    if shape == "absolute":
        return str(path)
    if shape == "relative":
        return os.path.relpath(path)
    if shape == "dot-relative":
        # `./` in front, spelled with the separator rather than by joining a
        # `Path`: `Path("./x")` normalises the `.` away, and the `./` is the
        # whole point of this shape.
        return f".{os.sep}{os.path.relpath(path)}"
    return str(directory)


@pytest.mark.parametrize("shape", SHAPES)
def test_every_spelling_of_file_offers_what_nix_offers(
    shape: str, equivalence_file: Path, equivalence_directory: Path, pynix_bin: str
) -> None:
    """The four shapes of ``--file`` answer one set, and it is Nix's set.

    A directory is the shape that needs the autocall of ``default.nix``, and a
    relative path is the shape that depends on the working directory of the
    program rather than of the shell.
    """
    source = spell(shape, equivalence_file, equivalence_directory)
    baseline = nix_candidates(["build", "--file", source, "nixo"])
    assert baseline == {"nixos", "nixosLater"}, f"nix answered {baseline} for {source}"

    with_option = argcomplete_candidates(f"pynix build --file {source} --attr nixo", pynix_bin)
    assert with_option == baseline

    with_hash = argcomplete_candidates(f"pynix build --file {source}#nixo", pynix_bin)
    assert {candidate.removeprefix(f"{source}#") for candidate in with_hash} == baseline


def test_a_file_with_no_hash_leaves_the_names_to_the_shell(equivalence_file: Path, pynix_bin: str) -> None:
    """Before a ``#``, ``--file`` answers nothing, and that is the right answer.

    A shell offers file names when a program answers nothing, which is what a
    caller wants while they are still typing the path. `nix` gives the same
    answer at the same position: its completion kind there is ``filenames``.
    """
    partial = str(equivalence_file)[:-4]
    assert argcomplete_candidates(f"pynix build --file {partial}", pynix_bin) == set()
