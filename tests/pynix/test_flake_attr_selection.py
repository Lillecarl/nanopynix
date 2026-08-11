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
from pynix import Pynix

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.nix_environment import NixTestEnvironment

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
    cmd = Pynix.parse(["eval", "--flake", reference, *environment.pynix_store_args()])
    await cmd.astart()
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
    cmd = Pynix.parse(["eval", "--flake", f"{search_flake}#absent", *shared_nix_environment.pynix_store_args()])

    with pytest.raises(SystemExit):
        await cmd.astart()

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
    cmd = Pynix.parse(
        ["eval", "--flake", str(search_flake), "--attr", "nested", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.astart()

    # The search resolved the empty fragment to `packages.<system>.default`,
    # which is a string, so `--attr nested` has no attribute set to look in.
    assert "expected a set" in strip_ansi(capsys.readouterr().err)
