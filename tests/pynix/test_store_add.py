from __future__ import annotations

import json
from pathlib import Path

import nanopynix
import pytest
from nanopynix_proto.nix.store import AddToStoreRequest, ComputeStorePathRequest

from pynix import Pynix


async def test_nanopynix_add_to_store_imports_file(empty_store: dict[str, str | Path | object], tmp_path: Path):
    source = tmp_path / "message.txt"
    source.write_text("nanopynix-add-file\n")

    async with nanopynix.Session() as nix, nix.store(str(empty_store["store_url"])) as store:
        computed = await store.compute_store_path(
            ComputeStorePathRequest(path=str(source), method="flat", hash_algo="sha256")
        )
        added = await store.add_to_store(AddToStoreRequest(path=str(source), method="flat", hash_algo="sha256"))

    assert added.path == computed.path
    physical_path = _physical_store_path(empty_store, added.path)
    assert physical_path.read_text() == "nanopynix-add-file\n"


async def test_pynix_store_add_file_imports_and_can_be_read(
    empty_store: dict[str, str | Path | object],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "message.txt"
    source.write_text("pynix-add-file\n")

    cmd = Pynix.parse(["store", "add-file", str(source), "--store", str(empty_store["store_url"])])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert _physical_store_path(empty_store, data["path"]).read_text() == "pynix-add-file\n"


async def test_pynix_store_add_path_imports_directory(empty_store: dict[str, str | Path | object], tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    source = tmp_path / "source-dir"
    (source / "share").mkdir(parents=True)
    (source / "share" / "message").write_text("pynix-add-path\n")

    cmd = Pynix.parse(
        ["store", "add-path", str(source), "--name", "custom-dir", "--store", str(empty_store["store_url"])]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].endswith("-custom-dir")
    assert (_physical_store_path(empty_store, data["path"]) / "share" / "message").read_text() == "pynix-add-path\n"


async def test_pynix_store_add_dry_run_does_not_import(empty_store: dict[str, str | Path | object], tmp_path: Path, capsys: pytest.CaptureFixture[str]):
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
            "--store",
            str(empty_store["store_url"]),
        ]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert not _physical_store_path(empty_store, data["path"]).exists()


def _physical_store_path(empty_store: dict[str, str | Path | object], store_path: str) -> Path:
    store_root = empty_store["store_root"]
    if not isinstance(store_root, Path):
        raise TypeError("empty_store['store_root'] must be a Path")
    return store_root / store_path.removeprefix("/")
