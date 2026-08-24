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

import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from _completion_probe import argcomplete_candidates, nix_candidates, nix_environment

if TYPE_CHECKING:
    from collections.abc import Iterator
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

#: The identifier that `user_registry` below puts in a registry of its own.
PROBE_ENTRY = "aprobeflake"

#: The flake references to ask both programs about, before any `#`.
#:
#: Each one is a shape a caller types: nothing at all, the stem of the entry
#: the fixture writes, the same stem with the scheme that `completeFlakeRef`
#: strips, a stem of the *machine's* registry, a stem that names no entry at
#: all, and a relative path.
#:
#: `nixp` earns its row on a developer machine, where it reaches the system
#: and global layers that the fixture does not touch. In a build sandbox those
#: layers are absent and both programs answer nothing, which is still equal.
#: That is why the fixture exists: an equal pair of empty answers is not
#: evidence, and `PROBE_ENTRY` is there in every environment.
REFERENCES = ("", "aprobe", "flake:aprobe", "nixp", "zzzz-no-such-entry", "./")

#: The characters bash splits a word at, as `_completion_probe` passes them.
#: Only the ones a flake reference can hold matter, and `:` is the one that
#: does.
BASH_WORDBREAKS = " \t\n\"'><=;|&(:"


@pytest.fixture(scope="session", autouse=True)
def user_registry(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A user registry that both programs read, with one entry this file owns.

    **`NIX_CONFIG_HOME`, and not a setting.** `getConfigDir` (`libutil/users.cc`)
    reads that variable first, and `getUserRegistryPath` is that directory plus
    `registry.json`. So it reaches `nix` and this program alike -- where
    `flake-registry` in a `nix.conf` reaches only `nix`, because nanopynix
    registers no fetch settings with `globalConfig`. Issue #234 holds that.

    **Without it the rows below pass on nothing.** A build sandbox has no
    `/etc/nix/registry.json`, no `$HOME/.config`, and no network for the global
    layer, so both programs answer nothing for every registry prefix and every
    row compares an empty set with an empty set.
    """
    directory = tmp_path_factory.mktemp("user-registry")
    (directory / "registry.json").write_text(
        json.dumps(
            {
                "version": 2,
                "flakes": [
                    {
                        "from": {"type": "indirect", "id": PROBE_ENTRY},
                        "to": {"type": "github", "owner": "an-owner", "repo": "a-repo"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    patch = pytest.MonkeyPatch()
    patch.setenv("NIX_CONFIG_HOME", str(directory))
    yield directory
    patch.undo()


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


def after_wordbreak(candidates: set[str], reference: str) -> set[str]:
    """Each candidate with the part bash already has in front of it removed.

    **A raw candidate of one program is not a raw candidate of the other, and
    the command line is still the same.** bash splits a word at every character
    of `COMP_WORDBREAKS`, which holds `:`, and it replaces only the part after
    the last one. argcomplete knows that and strips each candidate to that
    part, so `flake:nixp<TAB>` comes back as `nixpkgs`. `nix` is asked here
    through `NIX_GET_COMPLETIONS`, which no shell has split, so it answers
    `flake:nixpkgs`. bash writes the same line either way.

    Measured: without this the `flake:nixp` row is the only one of five that
    fails, and it fails on every candidate.
    """
    cut = max((reference.rfind(character) for character in BASH_WORDBREAKS), default=-1)
    if cut < 0:
        return candidates
    head = reference[: cut + 1]
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


@pytest.mark.parametrize("reference", REFERENCES)
def test_a_flake_reference_offers_what_nix_offers(reference: str, pynix_bin: str) -> None:
    """Before the `#`, both programs offer a registry entry or a directory.

    `completeFlakeRef` (`libcmd/installables.cc`) reads three sources: the
    bare `.` for an empty prefix, `Args::completeDir`, and every layer of the
    registry. Issue #229 gave this program the third one, and the first two
    with it -- a completer that answers cannot let the shell fall back to file
    names.

    **A superset, and not an equality, and the difference is deliberate.**
    `getRegistries` builds all four layers before it returns any of them, so
    `nix` discards the user and system layers whenever the global one raises
    -- which is every machine with no network, this gate included.
    `_registry_references` asks again without the global layer and answers
    from the rest, so it can offer a candidate `nix` does not.

    The direction that matters is still pinned: a candidate `nix` offers and
    this program does not fails the row. That is the regression this table was
    written to catch. `pynix/tests/test_completion_registry.py` pins the other
    direction, with a control that goes red when the second call is removed.
    """
    baseline = after_wordbreak(nix_candidates(["build", reference], FLAKE_COMPLETION_INDEX), reference)
    ours = argcomplete_candidates(f"pynix build --flake {reference}", pynix_bin)
    assert baseline <= ours, f"nix offers what this program does not: {sorted(baseline - ours)}"


def test_the_reference_rows_are_not_all_empty(
    pynix_bin: str, user_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the table above.

    Every row there compares two answers, so two programs that both answered
    nothing would pass all of them. `user_registry` puts one entry where both
    programs read it, and this states that the entry really comes back.

    **The failure carries the traceback, and that is not decoration.** A
    completion that fails answers nothing and says nothing -- the module
    docstring of `pynix._attr_completion` says so, because a traceback drawn
    into a command line is worse than a missing candidate.
    `PYNIX_COMPLETION_DEBUG` is the way to look, and a control that fails in a
    build sandbox is exactly the case where nobody can look by hand. Measured:
    without this, three sandbox runs reported `assert 'aprobeflake' in set()`
    and named no cause.
    """
    record = tmp_path / "completion-failure.txt"
    monkeypatch.setenv("PYNIX_COMPLETION_DEBUG", str(record))

    ours = argcomplete_candidates("pynix build --flake aprobe", pynix_bin)

    assert PROBE_ENTRY in ours, (
        f"offered {sorted(ours)}\n"
        f"NIX_CONFIG_HOME={user_registry}\n"
        f"registry.json exists={(user_registry / 'registry.json').is_file()}\n"
        f"nix offers {sorted(nix_candidates(['build', 'aprobe'], FLAKE_COMPLETION_INDEX))}\n"
        f"completion traceback:\n{record.read_text(encoding='utf-8') if record.is_file() else '(none recorded)'}"
    )


def test_a_directory_reaches_the_answer_too(equivalence_flake: Path, pynix_bin: str) -> None:
    """The second control, for the other source.

    The test above pins a registry entry, and a completer that read the
    registry alone would pass it. The fixture directory is in no registry, so
    only the glob can offer it.
    """
    partial = str(equivalence_flake)[:-3]
    assert str(equivalence_flake) in argcomplete_candidates(f"pynix build --flake {partial}", pynix_bin)


def test_a_command_with_no_search_reads_the_fragment_as_one_path(
    equivalence_flake: Path, pynix_bin: str, current_system: str
) -> None:
    """`pynix search` applies no prefix, and `nix` has no subcommand like it.

    So this row states the behaviour rather than comparing it. No prefix means
    the top of the flake and nothing else, and no defaults means the empty
    fragment offers no candidate of its own -- which `nix build` does offer,
    because `packages.<system>.default` is there.
    """
    word = f"{equivalence_flake}#"
    ours = bare(argcomplete_candidates(f"pynix search --flake {word}", pynix_bin), equivalence_flake)
    assert ours == {"packages", "legacyPackages", "devShells", "topone", "toptwo"}

    with_a_default = bare(argcomplete_candidates(f"pynix build --flake {word}", pynix_bin), equivalence_flake)
    assert "" in with_a_default
    assert "" not in ours
    assert f"packages.{current_system}.default" not in ours
