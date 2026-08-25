"""The implementation of the ``pynix copy`` command.

``pynix.copy`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand
module on every start, and none of these imports is needed to list an option.

**Two stores, one session.** ``AsyncStore.copy_closure`` takes a destination
store and refuses one that another session opened, because a single Nix thread
drives both. So this command opens the session first and both stores under it,
rather than reaching for :func:`pynix._util.store_session`, which opens one.

**``--substitute-on-destination`` is not here.** ``copy_closure`` takes the
keyword, so adding the flag is one line. Issue #80 puts the substituter
protocol out of scope, and a flag that no test drives is worse than an absent
one. Add it with the test that proves it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from nanopynix._typechecking import BEARTYPING
from pynix._util import error_exit, nix_session, print_json, report_and_exit
from pynix.copy import Copy
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    base_attr_search,
    derivation_path,
    evaluate_target,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable
logger = structlog.get_logger("pynix.copy")


def _endpoints(command: Copy) -> tuple[str, str]:
    """The store to read from and the store to write to, in that order.

    ``--store`` is the side that the caller did not name, which is how ``nix
    copy`` resolves the same pair. Naming both means ``--store`` takes no part.
    """
    if command.to is None and command.from_ is None:
        error_exit("name --to or --from: pynix copy needs a second store")
    source = command.from_ if command.from_ is not None else command.store
    destination = command.to if command.to is not None else command.store
    if source == destination:
        error_exit(f"--from and --to name the same store ({source})")
    return source, destination


async def _evaluated_paths(command: Copy, nix: Any, source: Any, source_uri: str) -> list[str]:
    """The output paths of the derivation that ``--file`` or ``--flake`` names.

    Empty when the caller named neither. ``pynix copy`` builds nothing, so an
    output that the source store does not hold is an error and not a build.
    """
    target = EvaluationTarget.from_command(command)
    try:
        target.validate()
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if target.file is None and target.flake is None:
        return []

    async with nix.eval(source) as session:
        try:
            root = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
            drv_path = await derivation_path(root, selected=target.selected_attr())
        except EvaluationTargetError as exc:
            report_and_exit(exc)

    outputs = [str(path) for path in await source.query_derivation_outputs(drv_path)]
    valid = [path for path in outputs if await source.is_valid_path(path)]
    if not valid:
        error_exit(f"{drv_path} has no output in {source_uri}: build it before you copy it")
    return valid


async def _closure(source: Any, paths: Iterable[str]) -> list[str]:
    """Every path that the copy will carry, sorted.

    ``compute_fs_closure`` takes one path, so a request for several is one
    call for each. Nix walks the references itself, which is why the command
    does not.
    """
    reached: set[str] = set()
    for path in paths:
        reached.update(str(member) for member in await source.compute_fs_closure(path))
    return sorted(reached)


async def run_copy(command: Copy) -> None:
    """The body of :meth:`pynix.copy.Copy.run`."""
    source_uri, destination_uri = _endpoints(command)

    async with nix_session() as nix, nix.store(source_uri) as source:
        requested = sorted({*command.paths, *await _evaluated_paths(command, nix, source, source_uri)})
        if not requested:
            error_exit("name a store path, or name --file or --flake")

        try:
            closure = await _closure(source, requested)
        except Exception as exc:
            # stderr, and not stdout: the output of this command is JSON, and
            # `pynix copy ... | jq` must not read this instead. `pynix.path_info`
            # carries the full account of `error_exit`.
            error_exit(str(exc), cause=exc)

        async with nix.store(destination_uri) as destination:
            # **Before the copy, and one question for each path.** Nix reports
            # nothing about what it wrote, so "what did this command copy" has
            # to be the difference between the two stores beforehand.
            # `query_missing` does not answer it: that one asks what a *build*
            # would still have to do, over derived paths and substituters.
            present = [path for path in closure if await destination.is_valid_path(path)]
            missing = [path for path in closure if path not in set(present)]
            logger.info(
                "pynix copy starting",
                source=source_uri,
                destination=destination_uri,
                requested=len(requested),
                closure=len(closure),
                missing=len(missing),
            )
            await source.copy_closure(list(requested), destination, check_sigs=command.check_sigs)

    print_json(
        {
            "from": source_uri,
            "to": destination_uri,
            "requested": requested,
            "copied": missing,
            "alreadyPresent": present,
        },
    )
