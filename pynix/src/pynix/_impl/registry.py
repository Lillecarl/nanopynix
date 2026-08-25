"""The implementation of the ``pynix registry`` command.

``pynix.registry`` holds the command classes and their options, and this
module holds what ``run`` needs. ``pynix._impl`` says why: the parser loads
every subcommand module on every start, and none of these imports is needed to
list an option.

**No command here names the flakes feature, and every one of them needs it.**
``parseFlakeRef`` refuses without it. The feature comes from the
configuration, which is where ``pynix flake`` takes it from as well, and
``nix registry`` needs the same thing from the same place. Naming it in the
call would configure the session, and a configured session is a second
session: ``pynix/tests/_shared_sessions.py`` gives that rule, and the inproc
engine allows one set of settings for each process.

**A write reads its file from disk, and not from Nix's per-process cache.**
``registry_add`` in ``nix_fetchers.cpp`` gives the whole reason: Nix keeps
each registry layer in a function-local static, so a program that writes twice
would build the second write on the first read. ``pynix registry list`` still
goes through the cached layers, because that is what agrees with ``nix
registry list``.
"""

from __future__ import annotations

import structlog

from nanopynix import attrs_to_python
from pynix._util import print_json, store_session
from pynix.registry import Add, List, Pin, Remove

logger = structlog.get_logger("pynix.registry")


async def run_list(command: List) -> None:
    """The body of :meth:`pynix.registry.List.run`."""
    async with store_session(command.store) as (_nix, store):
        entries = await store.registry_entries()
        path = await store.user_registry_path()
    print_json(
        {
            "userRegistry": path,
            "entries": [
                {
                    "type": entry.type,
                    "from": entry.from_,
                    "to": entry.to,
                    "exact": entry.exact,
                    "extraAttrs": attrs_to_python(entry.extra_attrs),
                }
                for entry in entries
            ],
        },
    )


async def run_add(command: Add) -> None:
    """The body of :meth:`pynix.registry.Add.run`."""
    async with store_session(command.store) as (_nix, store):
        write = await store.registry_add(command.from_ref, command.to_ref, path=command.registry)
    print_json({"path": write.path, "replaced": write.removed, "from": command.from_ref, "to": write.to})


async def run_remove(command: Remove) -> None:
    """The body of :meth:`pynix.registry.Remove.run`."""
    async with store_session(command.store) as (_nix, store):
        write = await store.registry_remove(command.from_ref, path=command.registry)
    print_json({"path": write.path, "removed": write.removed, "from": command.from_ref})


async def run_pin(command: Pin) -> None:
    """The body of :meth:`pynix.registry.Pin.run`.

    ``locked`` reports what Nix warns about. A reference with no revision
    pins to an entry that still moves, and the caller is the one who can tell
    whether that matters.
    """
    async with store_session(command.store) as (_nix, store):
        write = await store.registry_pin(command.from_ref, command.locked, path=command.registry)
    if write.locked is False:
        logger.warning("the pinned reference carries no revision, so it still moves", to=write.to)
    print_json(
        {
            "path": write.path,
            "replaced": write.removed,
            "from": command.from_ref,
            "to": write.to,
            "locked": write.locked,
        },
    )
