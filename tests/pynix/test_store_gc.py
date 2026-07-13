from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path

import pytest

from pynix import Pynix


def _store_path_basename(path: str) -> str:
    return path.split("/nix/store/", 1)[1]


async def test_print_roots(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "gc", "print-roots", "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "roots" in data
    assert isinstance(data["roots"], list)
    for root in data["roots"]:
        assert "link" in root
        assert "path" in root
        assert root["path"].startswith("/nix/store/")


async def test_print_alive(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "gc", "print-alive", "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert isinstance(data["paths"], list)
    for path in data["paths"]:
        assert path.startswith("/nix/store/")


async def test_print_dead_dry_run(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "gc", "print-dead", "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "paths" in data
    assert "bytesFreed" in data
    assert isinstance(data["paths"], list)
    assert isinstance(data["bytesFreed"], int)


def test_print_dead_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        Pynix.parse(["store", "gc", "print-dead", "--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--rip" in captured.out


async def test_path_from_hash_part(populated_store: dict[str, str], capsys):
    store_path = _store_path_basename(populated_store["hello_path"])
    hash_part = store_path.split("-", 1)[0]
    cmd = Pynix.parse(["store", "path-from-hash-part", hash_part, "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["path"] == f"/nix/store/{store_path}"


async def test_store_info(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "info", "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["uri"] == "local://"
    assert data["storeDir"] == "/nix/store"


async def test_is_valid_path(populated_store: dict[str, str], capsys):
    store_path = populated_store["hello_path"]
    cmd = Pynix.parse(["store", "is-valid-path", store_path, "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": store_path, "valid": True}


async def test_follow_links_to_store_path(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(
        ["store", "follow-links-to-store-path", populated_store["hello_path"], "--store", populated_store["store_url"]]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": populated_store["hello_path"]}


async def test_compute_fs_closure(populated_store: dict[str, str], capsys):
    store_path = populated_store["hello_path"]
    cmd = Pynix.parse(["store", "compute-fs-closure", store_path, "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert store_path in data["paths"]


async def test_query_missing(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(
        ["store", "query-missing", populated_store["hello_path"], "--store", populated_store["store_url"]]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["unknown"] == []
    assert data["willBuild"] == []
    assert data["willSubstitute"] == []
    assert isinstance(data["downloadSize"], int)
    assert isinstance(data["narSize"], int)


async def test_query_derivation_outputs(tmp_path, capsys):
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("""
    let
      pkgs = import <nixpkgs> {};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "query-outputs";
      version = "1";
      dontUnpack = true;
      installPhase = ''
        echo hi > "$out"
      '';
    }
    """)
    show = Pynix.parse(["derivation", "show", "--file", str(nix_file)])
    await show.astart()
    captured = capsys.readouterr()
    drv_path = next(iter(json.loads(captured.out)))

    cmd = Pynix.parse(["store", "query-derivation-outputs", drv_path, "--store", "auto"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data["paths"]) == 1
    assert data["paths"][0].startswith("/nix/store/")


async def test_query_valid_derivers(populated_store: dict[str, str], capsys):
    store_path = populated_store["hello_path"]
    cmd = Pynix.parse(["store", "query-valid-derivers", store_path, "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data["paths"], list)


async def test_list_valid_paths(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "list-valid-paths", "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert populated_store["hello_path"] in data["paths"]


async def test_query_referrers(populated_store: dict[str, str], capsys):
    store_path = populated_store["hello_path"]
    cmd = Pynix.parse(["store", "query-referrers", store_path, "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data["paths"], list)


async def test_query_substitutable_paths(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(
        ["store", "query-substitutable-paths", populated_store["hello_path"], "--store", populated_store["store_url"]]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data["paths"], list)


async def test_add_temp_root(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(
        ["store", "add-temp-root", populated_store["hello_path"], "--store", populated_store["store_url"]]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": populated_store["hello_path"], "added": True}


async def test_add_perm_root_and_indirect_root(populated_store: dict[str, str], tmp_path, capsys):
    root_path = tmp_path / "pynix-gc-root"
    cmd = Pynix.parse(
        [
            "store",
            "add-perm-root",
            populated_store["hello_path"],
            str(root_path),
            "--store",
            populated_store["store_url"],
        ]
    )
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": populated_store["hello_path"], "gcRoot": str(root_path)}
    assert root_path.is_symlink()

    cmd = Pynix.parse(["store", "add-indirect-root", str(root_path), "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": str(root_path), "added": True}


async def test_ensure_path(populated_store: dict[str, str], capsys):
    store_path = populated_store["hello_path"]
    cmd = Pynix.parse(["store", "ensure-path", store_path, "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"path": store_path, "valid": True}


async def test_store_cat_reads_file_from_populated_store(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "cat", populated_store["text_path"], "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    assert captured.out == "temporary-store-message\n"


async def test_store_ls_lists_populated_store_path(populated_store: dict[str, str], capsys):
    cmd = Pynix.parse(["store", "ls", populated_store["hello_path"], "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    assert "bin" in captured.out.splitlines()

    cmd = Pynix.parse(["store", "ls", populated_store["hello_path"], "--json", "--store", populated_store["store_url"]])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert {"name": "bin", "type": "directory"} in data["entries"]


async def test_store_cat_local_path_mapping_unit(tmp_path, monkeypatch, capsys):
    store_root = tmp_path / "store-root"
    store_url = f"local?root={store_root}"
    out_path = "/nix/store/00000000000000000000000000000000-pynix-store-cat-test"
    output_file = store_root / out_path.removeprefix("/") / "share" / "message"
    output_file.parent.mkdir(parents=True)
    output_file.write_text("cat-output\n")
    _install_fake_nanopynix(monkeypatch=monkeypatch, store_root=store_root)

    cmd = Pynix.parse(["store", "cat", f"{out_path}/share/message", "--store", store_url])
    await cmd.astart()
    captured = capsys.readouterr()
    assert captured.out == "cat-output\n"


async def test_store_ls_local_path_mapping_unit(tmp_path, monkeypatch, capsys):
    store_root = tmp_path / "store-root"
    store_url = f"local?root={store_root}"
    out_path = "/nix/store/00000000000000000000000000000000-pynix-store-ls-test"
    store_path = store_root / out_path.removeprefix("/")
    (store_path / "bin").mkdir(parents=True)
    (store_path / "share").mkdir()
    (store_path / "bin" / "tool").touch()
    (store_path / "share" / "message").touch()
    _install_fake_nanopynix(monkeypatch=monkeypatch, store_root=store_root)

    cmd = Pynix.parse(["store", "ls", out_path, "--store", store_url])
    await cmd.astart()
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["bin", "share"]

    cmd = Pynix.parse(["store", "ls", out_path, "--json", "--store", store_url])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "entries": [
            {"name": "bin", "type": "directory"},
            {"name": "share", "type": "directory"},
        ]
    }


async def test_store_diff_closures_reports_added_removed_and_size_delta(tmp_path, monkeypatch, capsys):
    before_path = "/nix/store/00000000000000000000000000000000-before"
    after_path = "/nix/store/11111111111111111111111111111111-after"
    common_path = "/nix/store/22222222222222222222222222222222-common"
    old_path = "/nix/store/33333333333333333333333333333333-old"
    new_path = "/nix/store/44444444444444444444444444444444-new"
    _install_fake_nanopynix(
        monkeypatch=monkeypatch,
        store_root=tmp_path / "store-root",
        closures={
            before_path: [common_path, old_path],
            after_path: [common_path, new_path],
        },
        nar_sizes={
            common_path: 10,
            old_path: 4,
            new_path: 7,
        },
    )

    cmd = Pynix.parse(["store", "diff-closures", before_path, after_path])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {
        "added": [{"narSize": 7, "path": new_path}],
        "after": after_path,
        "afterNarSize": 17,
        "before": before_path,
        "beforeNarSize": 14,
        "narSizeDelta": 3,
        "removed": [{"narSize": 4, "path": old_path}],
    }


async def test_optimise_empty_local_store(tmp_path, capsys):
    cmd = Pynix.parse(["store", "optimise", "--store", f"local?root={tmp_path}"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"optimised": True}


async def test_verify_empty_local_store(tmp_path, capsys):
    cmd = Pynix.parse(["store", "verify", "--store", f"local?root={tmp_path}"])
    await cmd.astart()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"errors": False}


def _install_fake_nanopynix(
    *,
    monkeypatch: pytest.MonkeyPatch,
    store_root: Path,
    closures: dict[str, list[str]] | None = None,
    nar_sizes: dict[str, int] | None = None,
) -> None:
    import pynix.store as store_module

    monkeypatch.setattr(store_module, "prepare_sys_path", lambda: None)
    monkeypatch.setattr(store_module, "forward_nix_logs", _noop_forward_nix_logs)
    monkeypatch.setitem(
        sys.modules,
        "nanopynix",
        SimpleNamespace(
            Session=lambda: _FakeSession(
                store_root,
                closures=closures or {},
                nar_sizes=nar_sizes or {},
            )
        ),
    )


@asynccontextmanager
async def _noop_forward_nix_logs(session: Any, *, print_build_logs: bool = False) -> AsyncGenerator[None, None]:  # noqa: ARG001 -- matches real forward_nix_logs signature
    yield


class _FakeSession:
    def __init__(self, store_root: Path, *, closures: dict[str, list[str]], nar_sizes: dict[str, int]) -> None:
        self._store_root = store_root
        self._closures = closures
        self._nar_sizes = nar_sizes

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def store(self, store_uri: str) -> _FakeStore:
        return _FakeStore(self._store_root, store_uri, closures=self._closures, nar_sizes=self._nar_sizes)

    async def log_stream(self) -> AsyncIterator[None]:
        if False:
            yield None


class _FakeStore:
    def __init__(
        self,
        store_root: Path,
        store_uri: str,
        *,
        closures: dict[str, list[str]],
        nar_sizes: dict[str, int],
    ) -> None:
        self._store_root = store_root
        self._store_uri = store_uri
        self._closures = closures
        self._nar_sizes = nar_sizes

    async def __aenter__(self) -> _FakeStore:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_store_dir(self, request: Any) -> SimpleNamespace:
        return SimpleNamespace(dir="/nix/store")

    async def query_path_info(self, request: Any) -> SimpleNamespace:
        if request.path in self._nar_sizes:
            return SimpleNamespace(nar_size=self._nar_sizes[request.path])
        physical_path = self._store_root / request.path.removeprefix("/")
        if not physical_path.exists():
            raise FileNotFoundError(request.path)
        return SimpleNamespace(nar_size=physical_path.stat().st_size)

    async def compute_fs_closure(self, request: Any) -> SimpleNamespace:
        paths = self._closures[request.path]
        return SimpleNamespace(paths=[SimpleNamespace(base_name=path.removeprefix("/nix/store/")) for path in paths])
