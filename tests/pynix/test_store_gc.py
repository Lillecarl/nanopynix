from __future__ import annotations

import json

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
    cmd = Pynix.parse(["store", "query-missing", populated_store["hello_path"], "--store", populated_store["store_url"]])
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
    builtins.derivation {
      name = "query-outputs";
      system = builtins.currentSystem;
      builder = "/bin/sh";
      args = [ "-c" "echo hi > $out" ];
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
    cmd = Pynix.parse(["store", "add-temp-root", populated_store["hello_path"], "--store", populated_store["store_url"]])
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
