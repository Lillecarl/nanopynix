from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from pynix import parse

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import NixTestEnvironment

_FIXTURE_DIR = Path(__file__).parent / "test_osearch"
_SYSTEM_NIX = _FIXTURE_DIR / "system.nix"


def _parse_json_output(out: str) -> object:
    """Extract the JSON portion from captured stdout, skipping structlog lines."""
    _structlog = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
    lines = [line for line in out.splitlines() if not _structlog.match(line)]
    return json.loads("".join(lines))


def _results(out: str) -> list[dict[str, object]]:
    """Parse an ``osearch --json`` result array into typed records."""
    data = _parse_json_output(out)
    if not isinstance(data, list):
        raise TypeError("expected osearch --json to print a JSON array")
    records: list[dict[str, object]] = []
    for entry in cast("list[object]", data):
        if not isinstance(entry, dict):
            raise TypeError("expected each osearch result to be a JSON object")
        records.append(cast("dict[str, object]", entry))
    return records


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point XDG_CACHE_HOME at a fresh per-test directory so tests don't share a cache."""
    cache_home = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    return cache_home


async def test_osearch_builds_index_and_finds_a_match(
    shared_nix_environment: NixTestEnvironment,
    cache_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "osearch",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "services.example-daemon.port",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    assert "indexed" in captured.err
    results = _results(captured.out)
    assert results
    assert str(results[0]["name"]) == "services.example-daemon.port"
    assert str(results[0]["type"]).startswith("16 bit unsigned integer")

    cache_files = list((cache_home / "pynix" / "osearch").glob("*.json"))
    assert len(cache_files) == 1


async def test_osearch_survives_an_option_whose_default_cannot_be_evaluated(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression test: a real-world module (e.g. disko) can define an option's
    default as an expression over `config` that only resolves once a whole
    system is realized. Indexing must not evaluate `default`/`example` at
    all, so such an option (`brokenDefault` in the fixture module) doesn't
    abort the entire bulk fetch, and the rest of the options still index."""
    cmd = parse(
        [
            "osearch",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "--limit",
            "100",
            "example-daemon",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    names = {str(result["name"]) for result in results}
    assert "services.example-daemon.brokenDefault" in names
    assert "services.example-daemon.port" in names
    assert "default" not in results[0]


async def test_osearch_filters_out_internal_options(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "osearch",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "--limit",
            "100",
            "secretInternal",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    assert all("secretInternal" not in str(result["name"]) for result in results)


async def test_osearch_second_run_hits_the_cache_without_a_working_store(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_cmd = parse(["osearch", "--file", str(_SYSTEM_NIX), *shared_nix_environment.pynix_store_args()])
    await build_cmd.run()
    capsys.readouterr()

    # A bogus store URI would make evaluation fail if the cache were bypassed.
    cached_cmd = parse(
        [
            "osearch",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "services.example-daemon.enable",
            "--store",
            "local://?root=/nonexistent-store-root",
        ],
    )
    await cached_cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    assert str(results[0]["name"]) == "services.example-daemon.enable"


async def test_osearch_update_index_rebuilds_the_cache(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_cmd = parse(["osearch", "--file", str(_SYSTEM_NIX), *shared_nix_environment.pynix_store_args()])
    await build_cmd.run()
    capsys.readouterr()

    rebuild_cmd = parse(
        ["osearch", "--file", str(_SYSTEM_NIX), "--update-index", *shared_nix_environment.pynix_store_args()],
    )
    await rebuild_cmd.run()
    captured = capsys.readouterr()
    assert "indexed" in captured.err


async def test_osearch_limit_truncates_results(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "osearch",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "--limit",
            "1",
            "example-daemon",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    assert len(results) == 1
