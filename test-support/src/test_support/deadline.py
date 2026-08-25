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

**The report also goes to a file, and that is not redundant.** A note on the
exception reaches a log only through pytest's end-of-run FAILURES section, and
the suite step of a test job is killed at ``timeout-minutes: 30``. Six tests
hanging for 120 s each spend 12 of those, so pytest never reaches that section
and the report is lost exactly when it matters. Issue #271 has run on CI many
times and no hang report of it has ever reached a log.

``NANOPYNIX_HANG_REPORT_FILE`` names a file that the job's upload step
collects. That step carries ``if: !cancelled()`` and the job timeout is far
above the step timeout, so it still runs after the suite step is killed.
"""

from __future__ import annotations

import contextlib
import functools
import os
from typing import TYPE_CHECKING

import anyio

from test_support.hang_report import hang_report

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: Seconds an async test may run before the wrapper fails it.
ASYNC_TEST_TIMEOUT = float(os.environ.get("NANOPYNIX_TEST_TIMEOUT", "120"))

#: Names a file that collects each hang report, so one survives a killed step.
#: ``ci/steps.nix`` sets it. Nothing is written when it is unset, which is the
#: local default.
HANG_REPORT_FILE_VAR = "NANOPYNIX_HANG_REPORT_FILE"


def _record(report: str, test: str) -> None:
    """Append one hang report to the collecting file, when there is one.

    Every error here is swallowed. This runs while a ``TimeoutError`` is on
    its way up, and a failure to write the report must not replace the
    timeout that the caller has to see.
    """
    path = os.environ.get(HANG_REPORT_FILE_VAR)
    if not path:
        return
    with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as handle:  # noqa: PTH123 -- pathlib has no append mode
        handle.write(f"=== hang report: {test} ===\n{report}\n")


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
            report = hang_report(deadline)
            _record(report, getattr(func, "__qualname__", repr(func)))
            exc.add_note(report)
            raise

    return wrapper
