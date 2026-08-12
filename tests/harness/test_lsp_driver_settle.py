"""``InProcessDriver`` waits for the warm-up that the server starts and drops.

``pynix._lsp._handlers._sync_document`` runs ``_warm_module_args`` as a
background task and returns without awaiting it. That is deliberate. The
docstring above it says a real editor cannot wait for the module arguments of a
file to resolve, because a completion popup has no such patience.

``InProcessDriver`` calls that function directly, so without a wait it returns
while the warm-up still runs. A hover on a module argument then answers
``None``, and the scenario reads a race as a defect.

**No other test notices when this wait goes away.** The scenarios in
``tests/pynix/test_lsp_scenarios.py`` pass on a development machine, where the
warm-up wins the race every time. They fail on a loaded CI runner. Run
31618055409 failed in two jobs and in two different ways -- one
``expected a hover result, got None`` and one 120s deadline -- while 14 jobs
that ran the same tests passed. That is what this file is here to stop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.support.lsp_drivers import InProcessDriver

if TYPE_CHECKING:
    from pynix._lsp._handlers import PynixLanguageServer


async def test_settle_waits_for_a_pending_warm_task(lsp_server: PynixLanguageServer) -> None:
    """A task parked in ``warm_tasks`` must finish before ``settle`` returns.

    The check does not sleep and does not depend on timing.
    ``asyncio.create_task`` schedules the coroutine and runs none of it, so
    ``warmed`` is still ``False`` on the line after it. Only an ``await`` of
    that task can set it, and ``settle`` is the one await in between.
    """
    warmed = False

    async def warm() -> None:
        nonlocal warmed
        warmed = True

    # An `asyncio.Task`, because that is what `warm_tasks` holds:
    # `_sync_document` puts one there for each document context it builds.
    task = asyncio.create_task(warm())
    lsp_server.warm_tasks.add(task)
    task.add_done_callback(lsp_server.warm_tasks.discard)

    assert not warmed, "create_task must not have run the coroutine yet, or this test proves nothing"

    await InProcessDriver(lsp_server).settle()

    assert warmed, "settle() returned while a warm-up task was still pending"
