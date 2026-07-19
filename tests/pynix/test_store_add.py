from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from nanopynix_proto.nix.store import AddToStoreRequest, ComputeStorePathRequest

from pynix import Pynix

if TYPE_CHECKING:
    import pytest

    from tests.support.nix_environment import NixTestEnvironment


async def test_nanopynix_add_to_store_imports_file(
    isolated_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    source = tmp_path / "message.txt"
    source.write_text("nanopynix-add-file\n")

    async with isolated_nix_environment.rpc_session() as nix, nix.store() as store:
        computed = await store.rpc.compute_store_path(
            ComputeStorePathRequest(path=str(source), method="flat", hash_algo="sha256")
        )
        added = await store.rpc.add_to_store(AddToStoreRequest(path=str(source), method="flat", hash_algo="sha256"))

    assert added.path == computed.path
    physical_path = _physical_store_path(isolated_nix_environment, added.path)
    assert physical_path.read_text() == "nanopynix-add-file\n"


async def test_pynix_store_add_file_imports_and_can_be_read(
    isolated_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "message.txt"
    source.write_text("pynix-add-file\n")

    cmd = Pynix.parse(["store", "add-file", str(source), *isolated_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert _physical_store_path(isolated_nix_environment, data["path"]).read_text() == "pynix-add-file\n"


async def test_pynix_store_add_path_imports_directory(
    isolated_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source-dir"
    (source / "share").mkdir(parents=True)
    (source / "share" / "message").write_text("pynix-add-path\n")

    cmd = Pynix.parse(
        ["store", "add-path", str(source), "--name", "custom-dir", *isolated_nix_environment.pynix_store_args()]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].endswith("-custom-dir")
    assert (
        _physical_store_path(isolated_nix_environment, data["path"]) / "share" / "message"
    ).read_text() == "pynix-add-path\n"


async def test_pynix_store_add_dry_run_does_not_import(
    isolated_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "message.txt"
    source.write_text("dry-run\n")

    cmd = Pynix.parse(
        [
            "store",
            "add",
            str(source),
            "--mode",
            "flat",
            "--dry-run",
            *isolated_nix_environment.pynix_store_args(),
        ]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert not _physical_store_path(isolated_nix_environment, data["path"]).exists()


def _physical_store_path(environment: NixTestEnvironment, store_path: str) -> Path:
    return environment.root / store_path.removeprefix("/")
