"""The implementation of the ``pynix path-info`` command.

``pynix.path_info`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from nanopynix._typechecking import BEARTYPING
from pynix._util import error_exit, print_json, store_session
from pynix.path_info import PathInfo

if TYPE_CHECKING or BEARTYPING:
    import nanopynix
logger = structlog.get_logger("pynix.path_info")


async def run_path_info(command: PathInfo) -> None:
    """The body of :meth:`pynix.path_info.PathInfo.run`."""
    async with store_session(command.store) as (_nix, store):
        try:
            info: nanopynix.PathInfo = await store.query_path_info(command.path)
        except Exception as exc:
            # stderr, and not stdout: the output of this command is JSON,
            # and `pynix path-info ... | jq` must not read this instead.
            #
            # `error_exit` turns the message into a `Text`, which is what
            # keeps the colour of Nix. See that function for the reason
            # that interpolation loses it.
            error_exit(str(exc), cause=exc)
            raise SystemExit(1) from exc
        result = {
            "path": info.path or command.path,
            "narHash": info.nar_hash,
            "narSize": info.nar_size,
            "references": list(info.references),
            "registrationTime": info.registration_time,
            "ultimate": info.ultimate,
            "ca": info.ca,
        }
        if info.deriver is not None:
            result["deriver"] = info.deriver
        print_json(result)
