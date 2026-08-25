"""No test drives a prompt_toolkit application through ``run_async`` itself.

``Application.run_async`` installs ``Application._handle_exception`` as the
exception handler of the **event loop**, for every task on that loop. That
handler prints the traceback and then waits for a keypress:

.. code-block:: python

    await _do_wait_for_enter("Press ENTER to continue...")

Nobody presses ENTER on a CI runner, or in a pipe. ``_do_wait_for_enter`` also
runs an ``Application`` of its own on the same loop, which fails for the same
reason and starts another handler, so it feeds itself.

Measured, issue #271, CI run 32799936618: one test held 4681 of those waits
and 4681 applications, and the tests after it on the same loop reached 15712
of each. Nothing was slow. The loop could never finish, and the 120 s of each
test was the deadline of ``test_support.deadline``.

``SearchTui.run_application`` passes ``set_exception_handler=False``, and it is
the one place allowed to name ``run_async``. This module exists because the
first correction missed a call site: three were in
``pynix/tests/test_search_tui.py`` and a fourth was in
``pynix/tests/test_search.py``, inside the very test that fails first. A
convention that a machine can check belongs in ``tests/meta/``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: The repository root, three levels above this file.
_ROOT = Path(__file__).resolve().parents[2]

#: The one file that may name ``run_async``, and the method that does it.
_ALLOWED = "pynix/src/pynix/_impl/_search_tui.py"

#: A call of the method on an application, in any of the shapes a caller
#: writes: ``await app.run_async()``, ``start_soon(tui.application.run_async)``.
_CALL = re.compile(r"\.run_async\b")


def _is_code(line: str) -> bool:
    """*line* is code, and not a comment or a line of a docstring.

    The reason for this rule lives in prose beside every caller, so the method
    is named many times in text. A backtick marks that text: this repository
    writes an identifier in a docstring as `` `run_async` ``.
    """
    stripped = line.strip()
    return not stripped.startswith(("#", "*", '"', "'")) and "`" not in line


def _python_files() -> list[Path]:
    """Every Python file of the projects that may touch prompt_toolkit."""
    found: list[Path] = []
    for project in ("pynix", "pynix-lsp"):
        found.extend(sorted((_ROOT / project).rglob("*.py")))
    return [path for path in found if ".pytest-agent" not in path.parts and "__pycache__" not in path.parts]


@pytest.mark.static_gate
def test_only_run_application_names_run_async() -> None:
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(_ROOT).as_posix()
        if relative == _ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # A docstring or a comment may name the method, and must, because
            # the reason lives in prose beside the callers.
            if _CALL.search(line) and _is_code(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, (
        "these call `run_async` rather than `SearchTui.run_application`, so prompt_toolkit "
        "installs its exception handler and a failure waits for a keypress:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.static_gate
def test_the_allowed_call_turns_the_handler_off() -> None:
    """The one permitted call passes the argument this module exists for."""
    text = (_ROOT / _ALLOWED).read_text(encoding="utf-8")
    calls = [line.strip() for line in text.splitlines() if _CALL.search(line) and _is_code(line)]
    assert calls == ["await self.application.run_async(set_exception_handler=False)"], calls
