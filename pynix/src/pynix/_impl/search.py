"""The implementation of the ``pynix search`` command.

Search the NixOS options and the packages of one target.

``pynix.search`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand
module on every start, and none of these imports is needed to list an option.

**One target answers both searches.** ``pynix._search_target`` finds the
options tree, the package set and the ``lib`` of whatever the caller pointed
at, and reports which attribute path answered. A target that holds only one of
the two still answers the search it can answer.

**The cache holds what the evaluator found, so a warm search needs none.**
One file for each target holds the options and the two paths that the package
half needs: the source of nixpkgs, and the ``programs.sqlite`` that names the
binaries. Without those two a warm search would still evaluate ``pkgs`` to
read ``pkgs.path``, which is the whole cost that the cache exists to avoid.

**One field is not in the cache, and it opens an evaluator of its own.** An
option's ``default`` and ``example`` have to be forced to be read, and a
default that only a realized system can evaluate must not stop the walk, so
``pynix._options`` leaves both out of the index. ``_values`` gives the detail
pane a resolver that opens a session on the first request and keeps it for as
long as the interface is on the screen. A search that reads names and
descriptions still evaluates nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from rich.console import Console

from libpynix import human_at_terminal
from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError
from pynix import _impl
from pynix._impl._quiet import quiet_terminal
from pynix._option_search import rank as rank_options
from pynix._option_values import EvaluatorUnavailableError, OptionValues
from pynix._options import OptionRecord, fetch_option_doc_list, fetch_option_values
from pynix._package_search import SearchablePackage, join, rank as rank_packages
from pynix._packages import (
    cache_path as package_cache_path,
    indexed_packages,
    load_cache as load_packages,
    package_identity,
)
from pynix._programs import ProgramIndex, program_index_for
from pynix._search_merge import OPTION, PACKAGE, kind, make_merged_ranker, name as hit_name
from pynix._search_target import LIB_CHAIN, OPTIONS_CHAIN, PKGS_CHAIN, Resolved, SearchTarget, resolve
from pynix._util import error_console, error_exit, eval_session, print_json, report_and_exit
from pynix.search import Search
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    evaluate_target,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncGenerator

    from nanopynix import AsyncEvalSession, AsyncValue

logger = structlog.get_logger("pynix.search")
console = Console()

#: The shape of a cache file. A file of another version rebuilds, because a
#: field that an older `pynix` did not write is a field this one needs.
CACHE_VERSION = 2

#: What `_has` calls each half, in the message it prints.
_OPTIONS_TREE = "options tree"
_PACKAGE_SET = "package set"


@dataclass(frozen=True)
class Wanted:
    """Which of the two indexes the caller asked for.

    Neither flag means both, and that is the default the user asked for: a
    person types one word and gets the good answer from whichever index holds
    it. A flag narrows the search, and it never widens one.
    """

    options: bool
    packages: bool

    #: Whether a flag named the indexes, rather than the default choosing
    #: both. It decides what a missing half means.
    explicit: bool


@dataclass
class Cached:
    """What one target wrote to the cache, and what a warm search reads back."""

    options: list[OptionRecord] | None = None
    pkgs_path: str | None = None
    programs_db: str | None = None
    origin: str = ""
    system: str = ""
    paths: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Found:
    """The records that a search ranks, and where they came from."""

    options: list[OptionRecord]
    packages: list[SearchablePackage]
    subject: str


def wanted(command: Search) -> Wanted:
    """Which indexes *command* asks for. Neither flag means both."""
    if command.options or command.packages:
        return Wanted(options=command.options, packages=command.packages, explicit=True)
    return Wanted(options=True, packages=True, explicit=False)


def _cache_dir() -> Path:
    """Return (creating if needed) the XDG cache directory for search indexes."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = cache_home / "pynix" / "search"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(command: Search, target: EvaluationTarget) -> Path:
    canonical = "|".join(
        str(part)
        for part in (
            target.file,
            target.attr,
            target.flake,
            command.options_attr,
            command.lib_attr,
            command.pkgs_attr,
            _system(command),
            command.channel,
        )
    )
    key = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return _cache_dir() / f"{key}.json"


def _system(command: Search) -> str:
    """The system whose binaries the package index answers for."""
    if command.system is not None:
        return command.system
    machine = platform.machine()
    system = "linux" if platform.system() == "Linux" else "darwin"
    return f"{machine}-{system}"


def _target_description(target: EvaluationTarget) -> str:
    base = target.file if target.file is not None else (target.flake or "<unknown>")
    return f"{base}#{target.attr}" if target.attr else base


def _fields(path: Path) -> dict[str, object] | None:
    """The fields of the cache at *path*, or `None` for anything else.

    A cache is a convenience, so a file that is missing, truncated or written
    by another version is not an error: the caller evaluates again.
    `pynix._packages` reads its own cache the same way.
    """
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    fields = cast("dict[str, object]", payload)
    return fields if fields.get("version") == CACHE_VERSION else None


def _text(value: object) -> str | None:
    """*value* when it is a string, and `None` for anything else."""
    return value if isinstance(value, str) else None


def _options_of(value: object) -> list[OptionRecord] | None:
    """The option records in *value*, or `None` when it holds none."""
    if not isinstance(value, list):
        return None
    records: list[OptionRecord] = []
    for entry in cast("list[object]", value):
        if not isinstance(entry, dict):
            return None
        records.append(OptionRecord(**cast("dict[str, Any]", entry)))
    return records


def _paths_of(value: object) -> dict[str, str]:
    """The attribute path that answered for each half, from *value*."""
    if not isinstance(value, dict):
        return {}
    found = cast("dict[str, object]", value)
    return {key: text for key, text in found.items() if isinstance(text, str)}


def load_cache(path: Path) -> Cached:
    """What *path* holds, or an empty record when it holds nothing usable."""
    fields = _fields(path)
    if fields is None:
        return Cached()
    return Cached(
        options=_options_of(fields.get("options")),
        pkgs_path=_text(fields.get("pkgs_path")),
        programs_db=_text(fields.get("programs_db")),
        origin=_text(fields.get("origin")) or "",
        system=_text(fields.get("system")) or "",
        paths=_paths_of(fields.get("paths")),
    )


def save_cache(path: Path, target: EvaluationTarget, cached: Cached) -> None:
    """Write *cached* to *path*, whole."""
    payload = {
        "version": CACHE_VERSION,
        "target": _target_description(target),
        "options": [asdict(record) for record in cached.options] if cached.options is not None else None,
        "pkgs_path": cached.pkgs_path,
        "programs_db": cached.programs_db,
        "origin": cached.origin,
        "system": cached.system,
        "paths": cached.paths,
    }
    path.write_text(json.dumps(payload))


def _missing(cached: Cached, ask: Wanted) -> bool:
    """Say whether the cache lacks anything that *ask* needs.

    **The binaries index is not part of the answer.** A package search runs
    on the walk of the package set, and the binaries only add the question
    "which package installs this program". A cache that holds the walk and
    no binaries is complete, because asking again means downloading the
    channel again on every single search.
    """
    if ask.options and cached.options is None:
        return True
    return ask.packages and cached.pkgs_path is None


async def run_search(command: Search) -> None:
    """The body of :meth:`pynix.search.Search.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    ask = wanted(command)
    path = _cache_path(command, target)
    cached = Cached() if command.update_index else load_cache(path)
    if _missing(cached, ask):
        cached = await _build_index(command, target, ask, cached)
        save_cache(path, target, cached)

    found = _read(command, target, ask, cached)
    if _use_tui(command):
        # The interface draws the whole terminal, and the resolver of the
        # detail pane opens an evaluator while it is up. `quiet_terminal`
        # says what one stray line of stderr does to the screen.
        with quiet_terminal():
            # This attribute read is what imports the interface.
            # `pynix._impl` holds the PEP 562 table that defers it, so a
            # caller who gave a query pays for neither `prompt_toolkit` nor
            # the Markdown renderer.
            await _impl.merged_tui.browse(
                found.options,
                found.packages,
                subject=found.subject,
                initial_query=command.query or "",
                values=_values(command, target, found),
            )
        return

    if command.query is not None:
        _print(command, found, command.query)


def _read(command: Search, target: EvaluationTarget, ask: Wanted, cached: Cached) -> Found:
    """Turn the cache into the records that a search ranks."""
    options = cached.options or [] if ask.options else []
    packages: list[SearchablePackage] = []
    if ask.packages and cached.pkgs_path is not None:
        records = load_packages(package_cache_path(cached.pkgs_path)) or []
        binaries: Mapping[str, list[str]] = {}
        if cached.programs_db is not None:
            index = ProgramIndex(path=Path(cached.programs_db), system=_system(command), release="", revision="")
            binaries = index.binaries_by_package()
        packages = join(records, binaries)
    subject = _target_description(target)
    if cached.origin and ask.packages:
        subject = f"{subject}, packages from {cached.origin}"
    return Found(options=options, packages=packages, subject=subject)


def _values(command: Search, target: EvaluationTarget, found: Found) -> OptionValues | None:
    """The resolver that forces one option's `default`, on the first request.

    **It opens no evaluator here.** A warm search reads its whole index from
    the cache, and opening an evaluator to draw a list of names would charge
    every search the 5 s that the cache exists to avoid. The reader who
    selects an option is the one who waits, and only the first one waits.

    The session lives inside the task that serves the requests, and closes
    with the interface. :class:`OptionValues` says why it cannot live outside
    that task.
    """
    if not found.options:
        return None

    @asynccontextmanager
    async def open_tree() -> AsyncGenerator[AsyncValue]:
        async with eval_session(command.store) as (_nix, _store, session):
            yield await _option_tree(command, target, session)

    return OptionValues(open_tree)


async def _option_tree(command: Search, target: EvaluationTarget, session: AsyncEvalSession) -> AsyncValue:
    """Evaluate *target* again, and return its lazy attrset of option values.

    **This reports a failure as a `NixError`, and does not exit.** The
    interface is on the screen when this runs, so a call to `error_exit` would
    leave a full-screen application drawn over the message. The pane says what
    went wrong instead, in the one option the reader selected.
    """
    try:
        value = await evaluate_target(target, session, auto_call_file=True)
        where = await resolve(
            value,
            options_attr=command.options_attr,
            pkgs_attr=command.pkgs_attr,
            lib_attr=command.lib_attr,
        )
    except EvaluationTargetError as exc:
        raise EvaluatorUnavailableError(str(exc)) from exc
    if where.options is None or where.lib is None:
        raise EvaluatorUnavailableError(f"{_target_description(target)} holds no options tree to read a default from")
    return await fetch_option_values(session, where.options.value, where.lib.value)


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


async def _build_index(command: Search, target: EvaluationTarget, ask: Wanted, cached: Cached) -> Cached:
    """Evaluate the target, and fill in whatever the cache lacks."""
    built = Cached(
        options=cached.options,
        pkgs_path=cached.pkgs_path,
        programs_db=cached.programs_db,
        origin=cached.origin,
        system=_system(command),
        paths=dict(cached.paths),
    )
    async with eval_session(command.store) as (_nix, _store, session):
        try:
            value = await evaluate_target(target, session, auto_call_file=True)
            where = await resolve(
                value,
                options_attr=command.options_attr,
                pkgs_attr=command.pkgs_attr,
                lib_attr=command.lib_attr,
            )
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        if ask.options and built.options is None and _has(where.options, where.lib, ask, _OPTIONS_TREE):
            await _index_options(session, target, where, built)
        if ask.packages and built.pkgs_path is None and _has(where.pkgs, where.lib, ask, _PACKAGE_SET):
            await _index_packages(session, command, where, built)
    if built.options is None and built.pkgs_path is None:
        error_exit(
            f"{_target_description(target)} holds neither an options tree nor a package set. "
            f"Tried {', '.join(OPTIONS_CHAIN)} and {', '.join(PKGS_CHAIN)}; "
            "name one with --options-attr or --pkgs-attr."
        )
    return built


def _has(half: Resolved | None, lib: Resolved | None, ask: Wanted, what: str) -> bool:
    """Say whether this half of the search can run, and report when it cannot.

    **A flag makes a missing half an error, and the default does not.** A
    person who wrote `--packages` and pointed at a module system that hides
    its package set asked for something the target does not hold, and has to
    be told. A person who wrote no flag asked for whatever is there, so the
    search answers with the other half and says nothing.
    """
    if half is not None and lib is not None:
        return True
    if ask.explicit:
        chain = OPTIONS_CHAIN if what == _OPTIONS_TREE else PKGS_CHAIN
        flag = "--options-attr" if what == _OPTIONS_TREE else "--pkgs-attr"
        missing, tried, name_it = (
            (what, chain, flag)
            if half is None
            else ("nixpkgs lib", (*LIB_CHAIN, "the lib of the package set"), "--lib-attr")
        )
        error_exit(f"the target holds no {missing}: tried {', '.join(tried)}. Name one with {name_it}.")
    return False


async def _index_options(
    session: AsyncEvalSession,
    target: EvaluationTarget,
    where: SearchTarget,
    built: Cached,
) -> None:
    """Walk the options tree, and record which paths answered."""
    options = _named(where.options)
    lib = _named(where.lib)
    built.options = await fetch_option_doc_list(session, options.value, lib.value)
    built.paths["options"] = options.path
    built.paths["lib"] = lib.path
    error_console.print(
        f"indexed {len(built.options)} options from {_target_description(target)} ({options.path} and {lib.path})"
    )


async def _index_packages(
    session: AsyncEvalSession,
    command: Search,
    where: SearchTarget,
    built: Cached,
) -> None:
    """Walk the package set, find the binaries it installs, and record both."""
    pkgs = _named(where.pkgs)
    lib = _named(where.lib)
    records = await indexed_packages(session, pkgs.value, lib.value)
    built.pkgs_path = await package_identity(session, pkgs.value)
    built.paths["pkgs"] = pkgs.path

    # **The binaries are best effort, and the walk is not.** `programs.sqlite`
    # is in the channel expressions and in no git checkout, so a target that
    # is a flake input or a checkout has to download a channel to get one. A
    # machine with no network then answered nothing at all, where it holds
    # every package name already: measured in a build sandbox, a search of a
    # module-system fixture died with "the channel expressions hold no
    # programs.sqlite" and lost the 24 571 packages it had just walked.
    try:
        index = await program_index_for(session, Path(built.pkgs_path), built.system, command.channel)
    except (OSError, NixError) as exc:
        built.programs_db = None
        built.origin = ""
        error_console.print(f"indexed {len(records)} packages from {pkgs.path}, and no binaries: {exc}")
        return
    built.programs_db = str(index.path)
    built.origin = index.origin
    error_console.print(f"indexed {len(records)} packages from {pkgs.path}, binaries from {index.origin}")


def _named(found: Resolved | None) -> Resolved:
    """*found*, which `_has` already proved is there."""
    if found is None:
        raise ValueError("the resolver reported this half of the search as present")
    return found


def _print(command: Search, found: Found, query: str) -> None:
    """Print the best matches, tagged with the index that answered."""
    results = _ranked(found, query, command.limit)
    if command.json_output:
        print_json([_as_json(hit) for hit in results])
        return
    for hit in results:
        tag = "opt" if kind(hit) == OPTION else "pkg"
        console.print(f"[dim]{tag}[/dim] [bold]{hit_name(hit)}[/bold] :: {_summary(hit)}")


def _as_json(hit: OptionRecord | SearchablePackage) -> dict[str, object]:
    """One machine-readable row, tagged with the index that answered.

    A package writes the binaries it installs beside its record, because that
    is what the join added and a caller who asked "which package gives me
    `rg`" needs to read the answer back.
    """
    if isinstance(hit, OptionRecord):
        return {"kind": OPTION, **asdict(hit)}
    return {"kind": PACKAGE, **asdict(hit.record), "binaries": list(hit.binaries)}


def _summary(hit: OptionRecord | SearchablePackage) -> str:
    """One line about *hit*, for the list."""
    if isinstance(hit, OptionRecord):
        first = hit.description.strip().splitlines()[0] if hit.description else ""
        return f"{hit.type}{f' -- {first}' if first else ''}"
    description = hit.record.description or ""
    return f"{hit.record.version}{f' -- {description}' if description else ''}"


def _ranked(found: Found, query: str, limit: int) -> Sequence[OptionRecord | SearchablePackage]:
    """The best *limit* matches of *query*, over whichever indexes are there.

    A search over one index calls that index's own ranker, so a caller who
    passed `--options` reads exactly what the interface would draw.
    """
    if found.options and not found.packages:
        return rank_options(found.options, limit=limit)(query)
    if found.packages and not found.options:
        return rank_packages(found.packages)(query)[:limit]
    return make_merged_ranker(found.options, found.packages, limit=limit)(query)
