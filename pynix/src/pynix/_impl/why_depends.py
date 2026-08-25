"""The implementation of the ``pynix why-depends`` command.

``pynix.why_depends`` holds the command class and its options, and this module
holds what ``run`` needs. ``pynix._impl`` says why: the parser loads every
subcommand module on every start, and none of these imports is needed to list
an option.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import structlog

from nanopynix._typechecking import BEARTYPING
from pynix._util import error_exit, print_json, store_session
from pynix.why_depends import WhyDepends

if TYPE_CHECKING or BEARTYPING:
    from typing import Any
logger = structlog.get_logger("pynix.why_depends")


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
        # `compute_fs_closure` and the walk read the same references, so this
        # is unreachable unless the store changed under the command.
        error_exit(f"{command.package} does not depend on {command.dependency}")

    print_json({"package": command.package, "dependency": command.dependency, "chain": chain})
