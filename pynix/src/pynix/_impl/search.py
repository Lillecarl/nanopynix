"""The implementation of the ``pynix search`` command.

Search NixOS module options, using a cached, offline index.

``pynix.search`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import structlog
from rapidfuzz import fuzz, process
from rich.console import Console

from libpynix import human_at_terminal
from pynix import _impl
from pynix._options import OptionRecord, fetch_option_doc_list
from pynix._search_target import LIB_CHAIN, OPTIONS_CHAIN, Resolved, resolve
from pynix._util import error_console, error_exit, eval_session, print_json, report_and_exit
from pynix.search import Search
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    evaluate_target,
)

logger = structlog.get_logger("pynix.search")
console = Console()


def _cache_dir() -> Path:
    """Return (creating if needed) the XDG cache directory for search indexes."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = cache_home / "pynix" / "search"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(target: EvaluationTarget, options_attr: str | None, lib_attr: str | None) -> Path:
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


async def run_search(command: Search) -> None:
    """The body of :meth:`pynix.search.Search.run`."""
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

    if _use_tui(command):
        # This attribute read is what imports the interface. `pynix._impl`
        # holds the PEP 562 table that defers it, so a caller who gave a query
        # pays for neither `prompt_toolkit` nor the Markdown renderer.
        await _impl.options_tui.browse(
            records,
            subject=_target_description(target),
            initial_query=command.query or "",
        )
        return

    if command.query is not None:
        _search(command, records, command.query)


def _use_tui(command: Search) -> bool:
    """Say whether to open the full-screen interface rather than print a list.

    `--tui` and `--no-tui` answer this outright. Without one of them, the
    interface opens for a person at a terminal who gave no query, because a
    person then has nothing to read and a search to start. A query on the
    command line asks a question that a list answers, and `--json-output` asks
    for a machine-readable answer, so neither opens the interface.
    """
    if command.tui is not None:
        return command.tui
    if command.json_output or command.query:
        return False
    return human_at_terminal()


async def _build_index(command: Search, target: EvaluationTarget, cache_path: Path) -> list[OptionRecord]:
    async with eval_session(command.store) as (_nix, _store, session):
        try:
            target_value = await evaluate_target(target, session, auto_call_file=True)
            found = await resolve(
                target_value,
                options_attr=command.options_attr,
                lib_attr=command.lib_attr,
            )
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        options = _required(found.options, "options tree", OPTIONS_CHAIN, "--options-attr")
        lib = _required(found.lib, "nixpkgs lib", (*LIB_CHAIN, "<the lib of the package set>"), "--lib-attr")
        records = await fetch_option_doc_list(session, options.value, lib.value)
    _save_cache(cache_path, target, records)
    where = f"{options.path} and {lib.path}"
    error_console.print(f"indexed {len(records)} options from {_target_description(target)} ({where})")
    return records


def _required(found: Resolved | None, what: str, tried: Sequence[str], flag: str) -> Resolved:
    """*found*, or exit with a message that names every path that was tried.

    A target that holds no options tree is a real thing to point at, and a
    person who did it by mistake needs to read which paths the search used.
    """
    if found is None:
        candidates = ", ".join(tried)
        error_exit(f"the target holds no {what}: tried {candidates}. Name one with {flag}.")
    return found


def _search(command: Search, records: list[OptionRecord], query: str) -> None:
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
