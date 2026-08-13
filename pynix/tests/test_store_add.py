from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nanopynix_proto.nix.store import AddToStoreRequest, ComputeStorePathRequest

from pynix import Pynix

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from nanopynix_testing.nix_environment import NixTestEnvironment


async def test_nanopynix_add_to_store_imports_file(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> None:
    source = tmp_path / "message.txt"
    source.write_text("nanopynix-add-file\n")

    async with shared_nix_environment.rpc_session() as nix, nix.store() as store:
        computed = await store.rpc.compute_store_path(
            ComputeStorePathRequest(path=str(source), method="flat", hash_algo="sha256"),
        )
        added = await store.rpc.add_to_store(AddToStoreRequest(path=str(source), method="flat", hash_algo="sha256"))

    assert added.path == computed.path
    physical_path = shared_nix_environment.physical_path(added.path)
    assert physical_path.read_text() == "nanopynix-add-file\n"


async def test_pynix_store_add_file_imports_and_can_be_read(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "message.txt"
    source.write_text("pynix-add-file\n")

    cmd = Pynix.parse(["store", "add-file", str(source), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert shared_nix_environment.physical_path(data["path"]).read_text() == "pynix-add-file\n"


async def test_pynix_store_add_path_imports_directory(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source-dir"
    (source / "share").mkdir(parents=True)
    (source / "share" / "message").write_text("pynix-add-path\n")

    cmd = Pynix.parse(
        ["store", "add-path", str(source), "--name", "custom-dir", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].endswith("-custom-dir")
    assert (shared_nix_environment.physical_path(data["path"]) / "share" / "message").read_text() == "pynix-add-path\n"


async def test_pynix_store_add_dry_run_does_not_import(
    shared_nix_environment: NixTestEnvironment,
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
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert not shared_nix_environment.physical_path(data["path"]).exists()
