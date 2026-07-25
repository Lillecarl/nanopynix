"""Extra introspection output, added to a test on purpose while troubleshooting.

Two ways in, one implementation behind them:

```python
def test_thing(agent_notes):
    agent_notes.note(store=path, backend=backend)      # structured, queryable
    agent_notes.attach("payload.json", raw)            # too big for a log line
```

```python
from pytest_agent import note                          # from anywhere at all
note(where="deep in the library under test", value=x)
```

The module-level functions exist for the case the fixture can't serve: a probe
dropped five frames down inside the code under test, where threading a fixture
through every caller is not a change anybody wants to make and then unmake.
They find the running test themselves.

Everything a note goes into, it goes into at once: appended to the run's
``notes.jsonl`` immediately (so a probe survives the crash it was added to
investigate), collected into the test's record in ``index.jsonl`` (so
``jq`` can slice a whole run by it), written into the test's ``.log``, and
echoed in the end-of-run terminal summary. That last one is deliberate --
a note only readable from a file costs an extra turn to read, and the point
of a probe is to answer the question in the run that added it.

With agent mode off there is nowhere to record anything, so notes print
instead and attachments go under ``<agent-dir>/attachments/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from pytest_agent._capture import ATTACHMENT_DIR_SUFFIX, Note, nodeid_to_relpath
from pytest_agent._runtime import RUNTIME_PLUGIN_NAME

if TYPE_CHECKING:
    from pytest_agent._runtime import AgentRuntime

# Sessions whose runtime is currently active, innermost last. A list rather
# than one slot because pytest-agent's own tests run inner pytest sessions
# in-process: the inner session's pytest_unconfigure must restore the outer
# session's runtime, not clear the module state outright.
_ACTIVE: list[AgentRuntime] = []


def push_runtime(runtime: AgentRuntime) -> None:
    _ACTIVE.append(runtime)


def pop_runtime(runtime: AgentRuntime) -> None:
    if runtime in _ACTIVE:
        _ACTIVE.remove(runtime)


def _active_runtime() -> AgentRuntime | None:
    return _ACTIVE[-1] if _ACTIVE else None


def note(**fields: object) -> None:
    """Record ``key=value`` troubleshooting output for the running test.

    Callable from anywhere during a test -- the test body, a fixture, or the
    code under test. Values need not be JSON-serializable; anything else is
    recorded as its ``repr`` rather than raising.
    """
    runtime = _active_runtime()
    _record_notes(runtime, runtime.current_nodeid if runtime is not None else None, fields)


def attach(name: str, content: str | bytes) -> Path:
    """Write *content* to *name* in the running test's attachment directory.

    For output too big to belong in a log line -- a captured payload, a
    rendered diff, a dump. Returns the path written.
    """
    runtime = _active_runtime()
    nodeid = runtime.current_nodeid if runtime is not None else None
    return _write_attachment(runtime, nodeid, name, content)


def attachment_dir() -> Path:
    """The running test's attachment directory, created on demand.

    For output a test doesn't produce as a value in hand -- a subprocess
    writing files, a library that only knows how to write to a directory.
    Anything left here is listed in the test's record and log.
    """
    runtime = _active_runtime()
    nodeid = runtime.current_nodeid if runtime is not None else None
    return _resolve_attachment_dir(runtime, nodeid)


class AgentNotes:
    """The ``agent_notes`` fixture's object, bound to one test."""

    def __init__(self, nodeid: str, runtime: AgentRuntime | None) -> None:
        self.nodeid = nodeid
        self._runtime = runtime

    def note(self, **fields: object) -> None:
        """Record ``key=value`` troubleshooting output for this test."""
        _record_notes(self._runtime, self.nodeid, fields)

    def attach(self, name: str, content: str | bytes) -> Path:
        """Write *content* to *name* in this test's attachment directory."""
        return _write_attachment(self._runtime, self.nodeid, name, content)

    @property
    def dir(self) -> Path:
        """The directory this test's attached files go in, created on demand."""
        return _resolve_attachment_dir(self._runtime, self.nodeid)


@pytest.fixture
def agent_notes(request: pytest.FixtureRequest) -> AgentNotes:
    """Attach extra introspection output to this test while troubleshooting.

    ```python
    def test_something(agent_notes):
        agent_notes.note(resolved=path, backend=backend)
        agent_notes.attach("payload.json", raw_response)
    ```

    Notes are echoed in the end-of-run summary, written into the test's log,
    and recorded in ``index.jsonl`` as ``notes`` so a whole run can be sliced
    by them. Attachments land in ``agent_notes.dir`` and are listed the same
    way. See also the module-level ``pytest_agent.note()``, which needs no
    fixture and so works from inside the code under test.
    """
    nodeid = request.node.nodeid  # type: ignore[reportUnknownMemberType] -- pytest's own stubs type Node.nodeid as Any
    runtime = cast("AgentRuntime | None", request.config.pluginmanager.get_plugin(RUNTIME_PLUGIN_NAME))
    return AgentNotes(cast("str", nodeid), runtime)


def _record_notes(runtime: AgentRuntime | None, nodeid: str | None, fields: dict[str, object]) -> None:
    if runtime is None:
        # No agent mode, so no record to put this in. Printing keeps the note
        # somewhere it can still be found: pytest's own captured output.
        for key, value in fields.items():
            print(f"[pytest-agent] note {Note.of(key, value).line()}")
        return
    for key, value in fields.items():
        runtime.recorder.add_note(nodeid, key, value)


def _write_attachment(
    runtime: AgentRuntime | None,
    nodeid: str | None,
    name: str,
    content: str | bytes,
) -> Path:
    path = _resolve_attachment_dir(runtime, nodeid) / _safe_relpath(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if runtime is None:
        # Same reasoning as _record_notes: with nothing recording, the path
        # has to be printed or it can't be found.
        print(f"[pytest-agent] attachment: {path}")
    return path


def _safe_relpath(name: str) -> Path:
    """*name* as a relative path that cannot leave the attachment directory.

    Subdirectories are kept rather than flattened away: attaching
    ``logs/worker-1.log`` and ``logs/worker-2.log`` has to produce two files,
    and silently collapsing them to one name would lose the second. Anything
    that could point outside the directory is refused outright -- an
    attachment landing somewhere unexpected is worse than a loud error.
    """
    candidate = Path(name.strip())
    if not candidate.parts or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"attachment name {name!r} is not usable as a file name")
    return candidate


def _resolve_attachment_dir(runtime: AgentRuntime | None, nodeid: str | None) -> Path:
    if runtime is not None and nodeid is not None:
        return runtime.recorder.attachment_dir_for(nodeid)
    return _fallback_dir(runtime, nodeid)


def _fallback_dir(runtime: AgentRuntime | None, nodeid: str | None) -> Path:
    """Where attachments go with no run directory to put them in.

    Two cases reach here: agent mode is off entirely, and agent mode is on
    but nothing is running (a session-scoped fixture, say), where there is no
    test to file the attachment under. A fixed, overwritten-each-run
    directory, the same shape ``profile`` uses when agent mode is off.
    """
    if runtime is not None:
        root = runtime.root
    else:
        configured = Path(os.environ.get("PYTEST_AGENT_DIR", ".pytest-agent"))
        root = configured if configured.is_absolute() else Path.cwd() / configured
    rel = nodeid_to_relpath(nodeid) if nodeid is not None else Path("session")
    directory = root / "attachments" / rel.parent / f"{rel.name}{ATTACHMENT_DIR_SUFFIX}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
