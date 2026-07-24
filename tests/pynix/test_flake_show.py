from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pynix import Pynix

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.support.nix_environment import NixTestEnvironment


async def test_flake_show_root(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], git_flake: Path
) -> None:
    cmd = Pynix.parse(["flake", "show", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "greeting" in captured.out


async def test_flake_show_with_hash_attrpath(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = Pynix.parse(["flake", "show", f"{git_flake}#hello", *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "greeting" not in captured.out


async def test_flake_show_with_a_separate_attrpath_flag_navigates_further(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    """--attrpath narrows further than the flake_ref's own '#' fragment,
    a distinct code path from resolving the fragment alone (see the other
    test above)."""
    cmd = Pynix.parse(
        ["flake", "show", f"{git_flake}#hello", "--attrpath", "pname", *shared_nix_environment.pynix_store_args()]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    assert "greeting" not in captured.out
    assert "drvPath" not in captured.out


async def test_flake_metadata_json_does_not_write_lock_file(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], git_flake: Path
) -> None:
    lock_file = git_flake / "flake.lock"
    assert not lock_file.exists()

    cmd = Pynix.parse(["flake", "metadata", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["resolvedRef"] == str(git_flake)
    assert "description" in data
    assert isinstance(data["inputs"], dict)
    assert not lock_file.exists()


async def test_flake_info_aliases_metadata(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], git_flake: Path
) -> None:
    cmd = Pynix.parse(["flake", "info", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["resolvedRef"] == str(git_flake)
