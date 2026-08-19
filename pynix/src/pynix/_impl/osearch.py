"""The implementation of the ``pynix osearch`` command.

Search NixOS module options, using a cached, offline index.

``pynix.osearch`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import structlog
from rapidfuzz import fuzz, process
from rich.console import Console

from pynix._options import OptionRecord, fetch_option_doc_list
from pynix._util import error_console, eval_session, print_json, report_and_exit
from pynix.osearch import Osearch
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    evaluate_target,
    select_attr,
)

logger = structlog.get_logger("pynix.osearch")
console = Console()


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
    base = target.file if target.file is not None else (target.flake or "<unknown>")
    return f"{base}#{target.attr}" if target.attr else base


def _load_cache(path: Path) -> list[OptionRecord]:
    data = json.loads(path.read_text())
    return [OptionRecord(**entry) for entry in data["options"]]


def _save_cache(path: Path, target: EvaluationTarget, records: list[OptionRecord]) -> None:
    payload = {"target": _target_description(target), "options": [asdict(record) for record in records]}
    path.write_text(json.dumps(payload))


async def run_osearch(command: Osearch) -> None:
    """The body of :meth:`pynix.osearch.Osearch.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    cache_path = _cache_path(target, command.options_attr, command.lib_attr)
    records = (
        _load_cache(cache_path)
        if cache_path.exists() and not command.update_index
        else await _build_index(command, target, cache_path)
    )

    if command.query is not None:
        _search(command, records, command.query)


async def _build_index(command: Osearch, target: EvaluationTarget, cache_path: Path) -> list[OptionRecord]:
    async with eval_session(command.store) as (_nix, _store, session):
        try:
            target_value = await evaluate_target(target, session, auto_call_file=True)
            options_value = await select_attr(target_value, command.options_attr)
            lib_value = await select_attr(target_value, command.lib_attr)
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        records = await fetch_option_doc_list(session, options_value, lib_value)
    _save_cache(cache_path, target, records)
    error_console.print(f"indexed {len(records)} options from {_target_description(target)}")
    return records


def _search(command: Osearch, records: list[OptionRecord], query: str) -> None:
    by_name = {record.name: record for record in records}
    matches = process.extract(query, list(by_name), scorer=fuzz.WRatio, limit=command.limit)
    results = [by_name[name] for name, _score, _index in matches]
    if command.json_output:
        print_json([asdict(record) for record in results])
        return
    for record in results:
        console.print(f"[bold]{record.name}[/bold] :: {record.type}")
        if record.description:
            first_line = record.description.strip().splitlines()[0]
            console.print(f"  {first_line}")
