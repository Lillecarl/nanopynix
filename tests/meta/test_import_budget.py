"""``import nanopynix`` must not load an engine, and must stay under a budget.

Issue #123 measured the cost of importing this package: 786.4 ms, against
16.7 ms for a bare interpreter and 43.0 ms for the C extension alone. The
largest single cause that ``nanopynix`` owns was
``from nanopynix import inproc, rpc`` at the top of ``__init__.py``, so a
program that used one engine loaded both. A module ``__getattr__`` (PEP 562)
now imports each one at first use.

**Without this file the next eager import puts the cost back in silence.**
Nothing else fails: an eager import makes every test pass slightly slower, and
no assertion anywhere reads the import graph.

## Why this asserts a count and a name, and not a duration

A wall-clock budget would be the direct measurement and it is the wrong gate.
The suite runs on a shared CI runner, under ThreadSanitizer, under
AddressSanitizer and on a laptop, and a millisecond figure that holds on one
of those is noise on the next. A module count is the same number everywhere,
and it moves for exactly the reason this file cares about.

The name check is the sharper half. A count has to carry headroom, so a budget
that fits one new dependency also hides one re-eager engine. Asserting that
``nanopynix.rpc`` is absent from ``sys.modules`` has no headroom at all.

## Why this is a meta test and not a gate

``tests/AGENTS.md`` says a meta test reads the repository and finishes in
milliseconds, and this one starts an interpreter. It is here anyway, because
the two rules that would send it elsewhere do not fit: ``tests/gates/`` is for
a tool CI already runs and requires ``xfail(strict=False)``, so a gate there
cannot fail a run, and this budget has to. The subject is the repository as a
whole, which is what ``tests/meta/`` is for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from test_support.notes import note

#: The most modules ``import nanopynix`` may load.
#:
#: Measured at 526 when issue #123 made the engines lazy, down from 566. The
#: headroom is deliberate and small: a legitimate new dependency raises this
#: number in the same commit that adds it, and the reviewer sees the cost.
#: Raise it with a measurement in the commit message, and never to make a
#: failing run green.
MODULE_BUDGET = 560

#: Modules that ``import nanopynix`` must not load.
#:
#: Each is an engine, and a program uses one of them. ``__getattr__`` in
#: ``nanopynix/__init__.py`` imports either one at first attribute access.
FORBIDDEN_EAGER_MODULES = ("nanopynix.inproc", "nanopynix.rpc")

#: What the probe below prints, as one JSON line.
_PROBE = """
import json, sys
import nanopynix
before = sorted(name for name in sys.modules if name.startswith("nanopynix."))
count = len(sys.modules)
nanopynix.rpc
nanopynix.inproc
print(json.dumps({
    "count": count,
    "loaded": before,
    "rpc_resolves": "nanopynix.rpc" in sys.modules,
    "inproc_resolves": "nanopynix.inproc" in sys.modules,
}))
"""


def _probe() -> dict[str, object]:
    """Import ``nanopynix`` in a clean interpreter and report what it loaded.

    ``PYTHONPATH`` is removed rather than inherited. ``tests/support``
    puts ``tests/_subprocess_startup`` on it so that a spawned interpreter
    starts beartype and coverage, and both of those import modules of their
    own. Inheriting it would make the count depend on whether the suite ran
    with ``--cov``, which is the one thing a budget must not do.
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(  # noqa: S603 -- sys.executable and a literal script, no shell
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"the import probe exited {completed.returncode}:\n{completed.stderr}")
    return json.loads(completed.stdout)


def test_importing_the_package_loads_neither_engine() -> None:
    """The invariant with no headroom, and the reason this file exists."""
    result = _probe()
    loaded = result["loaded"]
    assert isinstance(loaded, list)
    note(loaded_nanopynix_modules=json.dumps(loaded))

    eager = [name for name in FORBIDDEN_EAGER_MODULES if name in loaded]

    assert not eager, (
        f"import nanopynix loaded {eager}, so every program pays for an engine it may not use. "
        "Reach the engine through nanopynix.__getattr__ instead of importing it at the top of "
        "__init__.py -- see the _LAZY_SUBMODULES comment there."
    )


def test_each_engine_still_resolves_on_first_use() -> None:
    """Laziness must not become absence.

    ``from nanopynix import rpc`` and ``nanopynix.rpc`` are both public, and
    ``IMPORT_FROM`` falls back to ``getattr`` on the module, so both reach
    ``__getattr__``. A typo in ``_LAZY_SUBMODULES`` would leave the name
    raising ``AttributeError``, and the test above would still pass.
    """
    result = _probe()

    assert result["rpc_resolves"] is True, "nanopynix.rpc did not import on attribute access"
    assert result["inproc_resolves"] is True, "nanopynix.inproc did not import on attribute access"


def test_the_package_stays_under_the_module_budget() -> None:
    """The count, which catches a new dependency that the names above cannot."""
    result = _probe()
    count = result["count"]
    assert isinstance(count, int)
    note(modules_after_import=count, module_budget=MODULE_BUDGET)

    assert count <= MODULE_BUDGET, (
        f"import nanopynix loads {count} modules, over the budget of {MODULE_BUDGET}. "
        "Issue #123 holds the measurements. Raise MODULE_BUDGET only with a measurement in "
        "the commit message that says what the new modules buy."
    )


def test_the_budget_is_not_slack() -> None:
    """A budget far above the real count would pass while measuring nothing.

    The guard every derived test in this directory carries. If the package
    ever drops well under the budget, the budget is the number to lower.
    """
    count = _probe()["count"]
    assert isinstance(count, int)

    assert count > MODULE_BUDGET - 100, (
        f"import nanopynix loads {count} modules against a budget of {MODULE_BUDGET}, "
        "so the budget no longer measures anything. Lower MODULE_BUDGET to just above the "
        "count, and record the new figure on issue #123."
    )
