"""A deadline around every async test, and the report it attaches on expiry.

An async test can hang rather than fail. A wedged subprocess or a pipe nobody
drains never raises; it just never wakes the awaiting task, so the test, the
run and therefore the CI job all stop returning. This wrapper turns that into
a ``TimeoutError`` on the one test that hung.

A bare ``TimeoutError`` says the test took too long and nothing else. Five of
them in one CI job (`#44 <https://github.com/Lillecarl/nanopynix/issues/44>`_)
gave six lines between them, so the only next step was to run CI again.
:func:`~test_support.hang_report.hang_report` names the tasks and threads that
were still alive, which is the difference between evidence and a re-run.

**The environment variable keeps its ``NANOPYNIX_`` name.** This module is not
nanopynix-specific and the name says otherwise, but `ci/steps.nix` sets
``NANOPYNIX_TEST_TIMEOUT=300`` for the sanitized jobs, where everything is
slower. Renaming it here would leave that line setting a variable nothing
reads, and the sanitized jobs would silently fall back to 120 seconds. Read
that line before changing this one.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING

import anyio

from test_support.hang_report import hang_report

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: Seconds an async test may run before the wrapper fails it.
ASYNC_TEST_TIMEOUT = float(os.environ.get("NANOPYNIX_TEST_TIMEOUT", "120"))


def with_test_timeout(
    func: Callable[..., Awaitable[None]],
    timeout: float | None = None,
) -> Callable[..., Awaitable[None]]:
    """Wrap an async test so a hang becomes a ``TimeoutError`` that explains itself.

    Args:
        func: The coroutine function to wrap.
        timeout: Seconds to allow. Defaults to :data:`ASYNC_TEST_TIMEOUT`, read
            when the wrapper runs rather than when it is built, so a test can
            patch the module attribute and see the change.
    """

    @functools.wraps(func)
    async def wrapper(*args: object, **kwargs: object) -> None:
        deadline = ASYNC_TEST_TIMEOUT if timeout is None else timeout
        try:
            with anyio.fail_after(deadline):
                await func(*args, **kwargs)
        except TimeoutError as exc:
            exc.add_note(hang_report(deadline))
            raise

    return wrapper
