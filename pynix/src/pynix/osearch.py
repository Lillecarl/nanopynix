"""Search NixOS module options, using a cached, offline index."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import override

import structlog
from clypi import Command, Positional, arg
from rapidfuzz import fuzz, process
from rich.console import Console

import nanopynix
from pynix._options import OptionRecord, fetch_option_doc_list
from pynix._util import forward_nix_logs
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    evaluate_target,
    file_option,
    flake_option,
    select_attr,
)

logger = structlog.get_logger(__name__)
console = Console()
# Build-progress messages (e.g. "indexed N options") go to stderr, not
# stdout, so `--json-output`'s stdout stays clean, parseable JSON even when
# the cache also had to be (re)built as part of the same invocation.
console_err = Console(stderr=True)

_DEFAULT_STORE = "auto"


def _cache_dir() -> Path:
    """Return (creating if needed) the XDG cache directory for osearch indexes."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = cache_home / "pynix" / "osearch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(target: EvaluationTarget, options_attr: str, lib_attr: str) -> Path:
    canonical = f"{target.file}|{target.attr}|{target.flake}|{options_attr}|{lib_attr}"
    key = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return _cache_dir() / f"{key}.json"


def _target_description(target: EvaluationTarget) -> str:
    base = str(target.file) if target.file is not None else (target.flake or "<unknown>")
    return f"{base}#{target.attr}" if target.attr else base


def _load_cache(path: Path) -> list[OptionRecord]:
    data = json.loads(path.read_text())
    return [OptionRecord(**entry) for entry in data["options"]]


def _save_cache(path: Path, target: EvaluationTarget, records: list[OptionRecord]) -> None:
    payload = {"target": _target_description(target), "options": [asdict(record) for record in records]}
    path.write_text(json.dumps(payload))


class Osearch(Command):
    """Search NixOS module options, using a cached, offline index."""

    query: Positional[str | None] = arg(None, help="Search query. Omit to just (re)build the index.")
    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    options_attr: str = arg("options", help="Attribute path to the options tree, relative to the target.")
    lib_attr: str = arg("pkgs.lib", help="Attribute path to nixpkgs lib, relative to the target.")
    update_index: bool = arg(False, help="Re-evaluate and rebuild the cached index instead of using it.")
    limit: int = arg(20, help="Maximum number of results to print.")
    json_output: bool = arg(False, short="j", help="Print results as JSON instead of a human-readable list.")
    store: str = arg(_DEFAULT_STORE, help="Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        target = EvaluationTarget.from_command(self)
        try:
            target.validate(required=True)
        except EvaluationTargetError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise SystemExit(1) from exc

        cache_path = _cache_path(target, self.options_attr, self.lib_attr)
        records = (
            _load_cache(cache_path)
            if cache_path.exists() and not self.update_index
            else await self._build_index(target, cache_path)
        )

        if self.query is not None:
            self._search(records, self.query)

    async def _build_index(self, target: EvaluationTarget, cache_path: Path) -> list[OptionRecord]:
        async with (
            nanopynix.rpc.Session(experimental_features=["flakes", "nix-command"]) as nix,
            forward_nix_logs(nix),
            nix.store(self.store) as store,
            nix.eval(store) as session,
        ):
            try:
                target_value = await evaluate_target(target, session, auto_call_file=True)
                options_value = await select_attr(target_value, self.options_attr)
                lib_value = await select_attr(target_value, self.lib_attr)
            except EvaluationTargetError as exc:
                console.print(f"[red]Error:[/red] {exc}")
                raise SystemExit(1) from exc
            records = await fetch_option_doc_list(session, options_value, lib_value)
        _save_cache(cache_path, target, records)
        console_err.print(f"indexed {len(records)} options from {_target_description(target)}")
        return records

    def _search(self, records: list[OptionRecord], query: str) -> None:
        by_name = {record.name: record for record in records}
        matches = process.extract(query, list(by_name), scorer=fuzz.WRatio, limit=self.limit)
        results = [by_name[name] for name, _score, _index in matches]
        if self.json_output:
            sys.stdout.write(json.dumps([asdict(record) for record in results], sort_keys=True, indent=2))
            sys.stdout.write("\n")
            return
        for record in results:
            console.print(f"[bold]{record.name}[/bold] :: {record.type}")
            if record.description:
                first_line = record.description.strip().splitlines()[0]
                console.print(f"  {first_line}")
