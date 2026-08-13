"""The seam a test uses to kill an rpc worker on purpose.

``Session.close`` raises :class:`~nanopynix.WorkerSignaledError` when a signal
killed the worker and nothing in this process asked it to stop, so that a
crash inside a run cannot report success. See issue #55, where a full suite
reported 2077 passed with a core dump inside its own window.

A test that sends the signal itself is the one case where that report is
wrong, and this module is how such a test says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import nanopynix.rpc


def expect_the_worker_to_die(session: nanopynix.rpc.Session) -> None:
    """Tell ``session`` that the death about to happen is deliberate.

    Call it before the signal, so that no ordering question arises.

    **It gates the close only.** The call that was in flight when the signal
    landed still fails and still raises, because it really did fail. Every
    test that uses this asserts exactly that, and would not be worth keeping
    if this silenced it.
    """
    # This module *is* the seam, so it reaches past the public API on purpose.
    # See `WorkerClient.unexpected_death` for the flag that these two lines
    # set, and this module's docstring for why no public call can set it.
    manager: Any = session._manager  # type: ignore[reportPrivateUsage] -- the deliberate-death seam  # noqa: SLF001 -- same reason
    manager._expected_worker_death = True  # noqa: SLF001 -- same seam, one line later
