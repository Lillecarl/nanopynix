"""The flake attribute search of `pynix`, against a fixture flake.

`nix` does not read a fragment as one attribute path. `InstallableFlake`
builds a list of candidates from the prefixes and the defaults of the command,
and the first candidate that resolves is the answer. These tests drive
`pynix eval`, which carries the base pair of `SourceExprCommand`, and they
assert the value that each rule reaches.

The flake is local and its outputs are strings, so no test here builds
anything and none of them needs the network.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

import nanopynix
from nanopynix._ansi import strip_ansi
from pynix import parse

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment

_STRUCTLOG = re.compile(r"^\d{4}-\d{2}-\d{2}\s")


def _json_output(out: str) -> object:
    return json.loads("".join(line for line in out.splitlines() if not _STRUCTLOG.match(line)))


@pytest.fixture
def search_flake(tmp_path: Path) -> Path:
    """A flake whose outputs name every branch of the search.

    Each value says which branch reached it, so an assertion names the rule
    rather than a store path. `hello` sits in three places on purpose: under
    `packages`, under `legacyPackages` and at the top level.
    """
    flake_dir = tmp_path / "search-flake"
    flake_dir.mkdir()
    (flake_dir / "flake.nix").write_text("""
    {
      outputs = { ... }:
      let system = builtins.currentSystem; in
      {
        packages.${system} = {
          hello = "packages-hello";
          default = "packages-default";
        };
        legacyPackages.${system} = {
          hello = "legacy-hello";
          cold = "legacy-cold";
        };
        devShells.${system}.default = "devshell-default";
        hello = "top-level-hello";
        nested = { "dotted.name" = "quoted-name"; };
        notAnAttrset = "a string";
      };
    }
    """)
    return flake_dir


async def _eval(
    environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    reference: str,
) -> object:
    cmd = parse(["eval", "--flake", reference, *environment.pynix_store_args()])
    await cmd.run()
    return _json_output(capsys.readouterr().out)


async def test_a_fragment_finds_the_packages_prefix_first(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """`packages.<system>.hello` beats both `legacyPackages` and the top level."""
    assert await _eval(shared_nix_environment, capsys, f"{search_flake}#hello") == "packages-hello"


async def test_a_fragment_falls_through_to_legacy_packages(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """`cold` exists under `legacyPackages` only, which is the second prefix."""
    assert await _eval(shared_nix_environment, capsys, f"{search_flake}#cold") == "legacy-cold"


async def test_a_fragment_falls_through_to_the_bare_path(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """`notAnAttrset` is at the top level only, which is the last candidate."""
    assert await _eval(shared_nix_environment, capsys, f"{search_flake}#notAnAttrset") == "a string"


async def test_a_leading_dot_reaches_the_top_level(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """Without the dot this is `packages-hello`, so the dot is what is tested."""
    assert await _eval(shared_nix_environment, capsys, f"{search_flake}#.hello") == "top-level-hello"


async def test_no_fragment_takes_the_default_of_the_command(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """`packages.<system>.default`, and no prefix applies to a default."""
    assert await _eval(shared_nix_environment, capsys, str(search_flake)) == "packages-default"


async def test_a_quoted_component_holds_its_dot(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """Nix's path parser reads quotation marks, so a name may hold a dot."""
    reference = f'{search_flake}#.nested."dotted.name"'
    assert await _eval(shared_nix_environment, capsys, reference) == "quoted-name"


async def test_a_missing_fragment_names_every_candidate(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """The message of `InstallableFlake::getCursors`, and the names that exist."""
    system = nanopynix.current_system()
    cmd = parse(["eval", "--flake", f"{search_flake}#absent", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    error = strip_ansi(capsys.readouterr().err)
    assert "does not provide attribute" in error
    assert f"'packages.{system}.absent'" in error
    assert f"'legacyPackages.{system}.absent'" in error
    assert "'absent'" in error


async def test_the_attr_flag_still_selects_exactly_what_it_says(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """`--attr` is a plain path, applied after the search. No prefix reaches it."""
    cmd = parse(
        ["eval", "--flake", str(search_flake), "--attr", "nested", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()

    # The search resolved the empty fragment to `packages.<system>.default`,
    # which is a string, so `--attr nested` has no attribute set to look in.
    assert "expected a set" in strip_ansi(capsys.readouterr().err)


async def test_the_metadata_of_the_flake_is_not_an_attribute_of_the_target(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """`callFlake` merges the metadata in, and `nix` selects the outputs first.

    The value that `callFlake` returns holds `_type`, `inputs`, `lastModified`,
    `lastModifiedDate`, `narHash`, `outPath`, `outputs` and `sourceInfo` beside
    the outputs of the flake. `openEvalCache` of `src/libflake/flake.cc` takes
    `outputs` out of it before it resolves anything, so none of those eight is
    a target of `nix`.

    Measured before issue #228: `pynix eval --flake F#outPath` printed the
    source path of the flake, and `nix eval F#outPath` reported that the flake
    does not provide it. This asserts the message, because a failure for
    another reason would satisfy a bare `raises`.
    """
    cmd = parse(["eval", "--flake", f"{search_flake}#outPath", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    error = strip_ansi(capsys.readouterr().err)
    assert "does not provide attribute" in error
    assert "'outPath'" in error


async def test_the_outputs_attribute_is_not_a_target_either(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    search_flake: Path,
) -> None:
    """The control for the test above.

    `outputs` is the name that the selection resolves *through*, so a fix that
    stopped one level short would still reach it and would still hide every
    other name of the flake. It is not a target of `nix`, and reaching it would
    mean the target was rooted at the value of `callFlake` after all.
    """
    cmd = parse(["eval", "--flake", f"{search_flake}#outputs", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.run()

    assert "does not provide attribute" in strip_ansi(capsys.readouterr().err)
