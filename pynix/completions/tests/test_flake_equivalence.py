"""What `pynix` offers for a flake fragment, against what `nix` offers.

`test_nix_equivalence.py` beside this file does the same for `--file`, and its
docstring gives the reason the baseline is a program rather than a table.

**A flake fragment is not one attribute path, and that is why this is a
separate module.** `nix` resolves `#hello` against the prefixes of the command
first and the top of the flake last, so a completion has to offer the union of
those roots. `nix build` and `nix develop` disagree about what the prefixes
are, so the answer depends on the subcommand as well as on the flake --
neither of which is true of `--file`.

**The fixture holds a name under each root on purpose.** `pkgone` sits under
`packages` and under `legacyPackages`, `shellone` under `devShells` alone, and
`topone` at the top level. A completer that read one root and ignored the rest
would still pass a fixture whose names were all in one place.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from _completion_probe import argcomplete_candidates, nix_candidates, nix_environment

if TYPE_CHECKING:
    from pathlib import Path

#: Where the flake reference sits in `nix build F#frag`.
#: `nix` counts the arguments after the program name, and this line is
#: `build`(1) `F#frag`(2). One past the end ends the process in an assertion,
#: so the number is not a guess.
FLAKE_COMPLETION_INDEX = 2

#: The subcommands that both programs have, and the search each one applies.
#:
#: `nix build` takes the pair of `SourceExprCommand`. `nix develop` puts
#: `devShells.<system>` in front of it, and `nix repl` keeps the prefixes and
#: replaces the defaults with one empty path. Each name here is a subcommand of
#: both programs, so `nix` answers for every row.
COMMANDS = ("build", "develop", "repl")

#: The fragments to ask both programs about.
#:
#: Each one is a shape a caller types: nothing at all, a stem under a prefix, a
#: stem at the top level, a stem under one command's prefix only, the leading
#: dot that reaches past the prefixes, a trailing dot, and a name that only the
#: defaults hold.
FRAGMENTS = ("", "pkg", "top", "shell", ".top", "toptwo.", "default")


@pytest.fixture(scope="session")
def current_system() -> str:
    """The system that names each prefix of the search.

    Read out of `nix` rather than written down, because the prefixes are
    `packages.<system>.` and a hard-coded name would make every row of this
    module a test of one machine.
    """
    completed = subprocess.run(
        ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],  # noqa: S607 -- `nix` comes from the environment the gate builds
        env=nix_environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture(scope="session")
def equivalence_flake(tmp_path_factory: pytest.TempPathFactory, current_system: str) -> Path:
    """A flake that holds a name under each root of the search.

    **It has no inputs, so locking it fetches nothing.** A completion runs
    while a person holds a key down, and a fixture that fetched would measure
    the network.

    **Its own directory.** argcomplete's bash script ends in `complete -o
    default`, so a file beside the shell can come back as a candidate of the
    program.
    """
    directory = tmp_path_factory.mktemp("equivalence-flake")
    (directory / "flake.nix").write_text(
        "{\n"
        "  outputs = { self }: {\n"
        f'    packages.{current_system} = {{ pkgone = "one"; pkgtwo = "two"; default = "def"; }};\n'
        f'    legacyPackages.{current_system} = {{ legone = "leg"; pkgone = "shadowed"; }};\n'
        f'    devShells.{current_system}.shellone = "sh";\n'
        '    topone = "top";\n'
        '    toptwo = { deep = "d"; };\n'
        "  };\n"
        "}\n",
        encoding="utf-8",
    )
    return directory


def bare(candidates: set[str], reference: Path) -> set[str]:
    """Each candidate without the flake reference and the `#` in front of it.

    Both programs answer with the whole word, because a shell replaces the word
    under the cursor. The reference is the same in both answers, so comparing
    the fragments alone names the difference when a row fails.
    """
    head = f"{reference}#"
    return {candidate.removeprefix(head) for candidate in candidates}


@pytest.mark.parametrize("fragment", FRAGMENTS)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_flake_fragment_offers_what_nix_offers(
    command: str, fragment: str, equivalence_flake: Path, pynix_bin: str
) -> None:
    """One row for each subcommand and each shape of fragment.

    The subcommand is a parameter and not a constant, because the search that
    decides the answer belongs to the subcommand. `shell` is the fragment that
    separates them: `nix develop` offers `shellone` and `nix build` offers
    nothing for it.
    """
    word = f"{equivalence_flake}#{fragment}"
    baseline = bare(nix_candidates([command, word], FLAKE_COMPLETION_INDEX), equivalence_flake)
    ours = bare(argcomplete_candidates(f"pynix {command} --flake {word}", pynix_bin), equivalence_flake)
    assert ours == baseline


def test_the_fixture_separates_the_commands(equivalence_flake: Path, pynix_bin: str) -> None:
    """The control for the table above.

    Every row of the table compares two answers, so a fixture whose names sat
    under one root would let a completer that read one root pass all of them.
    `shellone` is under `devShells` alone, and this states that the two
    subcommands really do disagree about it.
    """
    word = f"{equivalence_flake}#shell"
    assert bare(argcomplete_candidates(f"pynix develop --flake {word}", pynix_bin), equivalence_flake) == {"shellone"}
    assert bare(argcomplete_candidates(f"pynix build --flake {word}", pynix_bin), equivalence_flake) == set()


def test_a_flake_with_no_hash_leaves_the_reference_to_the_shell(equivalence_flake: Path, pynix_bin: str) -> None:
    """Before a `#`, `--flake` answers nothing, so the shell offers file names.

    `nix` answers a flake reference at that position -- a registry entry, or a
    directory -- and this program does not. Issue #229 holds that half.
    """
    partial = str(equivalence_flake)[:-3]
    assert argcomplete_candidates(f"pynix build --flake {partial}", pynix_bin) == set()


def test_a_command_with_no_search_reads_the_fragment_as_one_path(
    equivalence_flake: Path, pynix_bin: str, current_system: str
) -> None:
    """`pynix osearch` applies no prefix, and `nix` has no subcommand like it.

    So this row states the behaviour rather than comparing it. No prefix means
    the top of the flake and nothing else, and no defaults means the empty
    fragment offers no candidate of its own -- which `nix build` does offer,
    because `packages.<system>.default` is there.
    """
    word = f"{equivalence_flake}#"
    ours = bare(argcomplete_candidates(f"pynix osearch --flake {word}", pynix_bin), equivalence_flake)
    assert ours == {"packages", "legacyPackages", "devShells", "topone", "toptwo"}

    with_a_default = bare(argcomplete_candidates(f"pynix build --flake {word}", pynix_bin), equivalence_flake)
    assert "" in with_a_default
    assert "" not in ours
    assert f"packages.{current_system}.default" not in ours
