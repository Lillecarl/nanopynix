"""The implementation of the ``pynix why-depends`` command.

``pynix.why_depends`` holds the command class and its options, and this module
holds what ``run`` needs. ``pynix._impl`` says why: the parser loads every
subcommand module on every start, and none of these imports is needed to list
an option.
"""

from __future__ import annotations

import functools
import itertools
import os
from collections import deque

# A real import, and not a `TYPE_CHECKING` one: `_scan_for_hash` builds a
# `Path` at run time.
from pathlib import Path
from typing import TYPE_CHECKING

import anyio.to_thread
import structlog

from nanopynix._typechecking import BEARTYPING
from nanopynix.models import StorePath
from pynix._util import error_exit, print_json, resolve_local_store_path, store_session
from pynix.why_depends import WhyDepends

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterator
    from typing import Any
logger = structlog.get_logger("pynix.why_depends")

#: Bytes of context on each side of a hit. The number `nix why-depends
#: --precise` uses, so the two print the same amount around the same hash.
_EXCERPT_MARGIN = 32


async def _shortest_chain(store: Any, package: str, dependency: str) -> list[str] | None:
    """The shortest chain of references from *package* to *dependency*.

    A breadth-first walk, so the first chain it reaches is a shortest one.
    ``None`` means that no chain exists.

    Nix records a path as a reference of itself whenever the content names its
    own store path, so the walk skips a node it has already reached. That also
    bounds the walk: the closure is finite, and each node enters the queue once.
    """
    if package == dependency:
        return [package]

    # The node a chain reaches each node through. `package` has no predecessor,
    # which is what ends the walk back.
    reached_from: dict[str, str | None] = {package: None}
    queue: deque[str] = deque([package])
    while queue:
        current = queue.popleft()
        info = await store.query_path_info(current)
        for reference in info.references:
            child = str(reference)
            if child in reached_from:
                continue
            reached_from[child] = current
            if child == dependency:
                return _chain_to(reached_from, child)
            queue.append(child)
    return None


def _chain_to(reached_from: dict[str, str | None], target: str) -> list[str]:
    """The chain from the start of the walk to *target*, in that order."""
    chain: list[str] = []
    node: str | None = target
    while node is not None:
        chain.append(node)
        node = reached_from[node]
    chain.reverse()
    return chain


def _entries(root: Path) -> Iterator[Path]:
    """Every file and symlink under *root*, and *root* itself when it is one.

    A store path is not always a directory. Both derivations of the test that
    covers this command write ``$out`` directly, so the path is one regular
    file, and a walk that assumed a directory would find nothing.

    A symlink is yielded and never followed. The reference lives in the target
    text, which :func:`_scan_for_hash` reads with ``readlink``, and descending
    into it would leave the same file scanned twice.
    """
    if root.is_symlink() or not root.is_dir():
        yield root
        return
    stack = [root]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or not entry.is_dir():
                    yield path
                else:
                    stack.append(path)


def _scan_for_hash(root: Path, hash_part: str) -> list[dict[str, str]]:
    """Each file under *root* that holds *hash_part*, with its context.

    **Blocking, and a caller runs the whole scan in one worker thread.** A
    store path holds thousands of files, and a hop per file through
    ``anyio.Path`` would cost thousands of thread round trips for a walk that
    is one syscall deep. One hop for the whole path keeps the event loop free
    and pays that cost once.

    One entry for each file, and the first occurrence in that file. Nix prints
    one line for each hit and reports every occurrence only under ``--all``,
    which issue #280 leaves out.
    """
    wanted = hash_part.encode()
    hits: list[dict[str, str]] = []
    for path in _entries(root):
        # `.` for the store path itself, which is what a store path that is
        # one regular file gives. Nix prints the absolute path in that case;
        # the caller already knows it, because it named it.
        name = "." if path == root else str(path.relative_to(root))
        if path.is_symlink():
            target = str(path.readlink())
            if hash_part in target:
                hits.append({"path": name, "target": target})
            continue
        contents = path.read_bytes()
        position = contents.find(wanted)
        if position >= 0:
            hits.append({"path": name, "excerpt": _excerpt(contents, position, len(wanted))})
    hits.sort(key=lambda hit: hit["path"])
    return hits


def _excerpt(contents: bytes, position: int, length: int) -> str:
    """The hit, with :data:`_EXCERPT_MARGIN` bytes of context on each side."""
    start = max(0, position - _EXCERPT_MARGIN)
    end = min(len(contents), position + length + _EXCERPT_MARGIN)
    return _printable(contents[start:end])


def _printable(raw: bytes) -> str:
    """*raw* with each byte that a terminal cannot show replaced by a dot.

    A store path holds ELF objects, and the bytes around a reference in one
    are arbitrary. Nix filters the same way, for the same reason.
    """
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in raw)  # noqa: PLR2004 -- the printable ASCII range


async def _precise_edges(store: Any, chain: list[str]) -> list[dict[str, Any]]:
    """For each link of *chain*, the files of the referrer that hold the referee.

    Empty for a chain of one node, which is what a path that depends on itself
    gives: there is no link to explain.
    """
    edges: list[dict[str, Any]] = []
    for referrer, referee in itertools.pairwise(chain):
        root = await resolve_local_store_path(store, referrer)
        hash_part = StorePath(referee).hash_part
        logger.info("pynix why-depends scanning", referrer=referrer, referee=referee)
        hits = await anyio.to_thread.run_sync(functools.partial(_scan_for_hash, root, hash_part))
        edges.append({"from": referrer, "to": referee, "hits": hits})
    return edges


async def run_why_depends(command: WhyDepends) -> None:
    """The body of :meth:`pynix.why_depends.WhyDepends.run`."""
    async with store_session(command.store) as (_nix, store):
        try:
            closure = {str(path) for path in await store.compute_fs_closure(command.package)}
        except Exception as exc:
            # stderr, and not stdout: the output of this command is JSON, and
            # `pynix why-depends ... | jq` must not read this instead.
            # `pynix.path_info` carries the full account of `error_exit`.
            error_exit(str(exc), cause=exc)
            raise SystemExit(1) from exc

        # The closure answers "no chain" in one walk of Nix's own, where the
        # search below would need one `query_path_info` round trip for every
        # path in that closure to learn the same thing.
        if command.dependency not in closure:
            error_exit(
                f"{command.package} does not depend on {command.dependency}: "
                f"the second path is not in the closure of the first",
            )

        chain = await _shortest_chain(store, command.package, command.dependency)

        if chain is None:
            # `compute_fs_closure` and the walk read the same references, so
            # this is unreachable unless the store changed under the command.
            error_exit(f"{command.package} does not depend on {command.dependency}")

        result: dict[str, Any] = {
            "package": command.package,
            "dependency": command.dependency,
            "chain": chain,
        }
        # Inside the block, because it reads the store. The key is absent
        # without the flag rather than empty, so a reader can tell "the
        # command did not look" from "the command looked and found nothing".
        if command.precise:
            result["references"] = await _precise_edges(store, chain)

    print_json(result)
