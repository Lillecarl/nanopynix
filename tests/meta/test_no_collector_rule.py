"""The rule that keeps an evaluator out of the pytest process stays accurate.

A build with ``enableGC = false`` leaks by design. Nix's own package option
says so, and it gives the condition under which that is acceptable: evaluation
takes place within short-lived processes. An RPC worker is such a process. The
pytest process is not, so :mod:`nanopynix_testing.nix_runtime` skips every test
that builds an evaluator inside it when the linked build has no collector.

**That rule has two decay modes, and this module gates both.**

The first is a rename. The rule finds most tests through the fixture closure
that pytest already computes, so it names two fixtures. If either name stops
resolving, the rule quietly matches fewer tests, and a rule that matches
nothing looks exactly like a rule with nothing to do.

The second is a new test module that builds an evaluator directly, with no
fixture in the closure to find it by. Nothing in the fixture graph records
that, so such a module carries ``evaluator_in_process`` by hand, and the
scanner below fails until it does.

**The scanner reports a construction, not an evaluator, and the ledger is
where that difference is recorded.** ``inproc.Session`` starts a session; the
evaluator arrives with ``Session.eval()``. A module that builds a session and
never evaluates costs the no-collector run nothing, and skipping it would lose
real coverage. Each such module is one :data:`LEDGER` entry with its reason,
rather than a silent exclusion in the scanner.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nanopynix_testing.nix_runtime import IN_PROCESS_EVALUATOR_FIXTURES, hosts_an_evaluator
from tests.support.suite_roots import SCANNED_ROOTS, check_roster
from tests.support.suppressions import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"

# Where a fixture may be *defined*, which is not where a test module lives.
# Issue #130 moved `inproc_session` and its peers into `nanopynix-testing`, and
# a scan of `tests/` alone then reported that the rule matched nothing. The
# roster is shared, so the next move edits one file.
FIXTURE_DIRS = SCANNED_ROOTS

# Modules that construct an in-process session and are still safe to run
# against a build with no collector. Each entry states why.
#
# `tests/nanopynix/inproc/test_inproc_process_env.py` is not here, and the
# reason is worth reading before adding it back. That module builds its
# session inside a source string that it hands to a subprocess, so the scanner
# never sees a construction and `test_every_ledger_entry_still_constructs`
# rejects the entry as stale. The outcome is correct either way: the session
# lives in a process that the test ends, so it returns its memory.
LEDGER: dict[str, str] = {
    "tests/nanopynix/test_error_boundaries.py": (
        "builds a Session over dummy:// for its store errors, and never calls eval()"
    ),
}


_SESSION_NAMES = frozenset({"Session", "EvalSession"})


def _bound_names(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """The names that ``tree`` bound from ``nanopynix.inproc``.

    Returns the module aliases, then the names bound directly. The rpc engine
    reuses ``Session`` and ``EvalSession``, so the import is the only thing
    that tells the two engines apart.
    """
    modules: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "nanopynix.inproc")
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if module == "nanopynix" and alias.name == "inproc":
                modules.add(bound)
            elif (module == "nanopynix" and alias.name == "EvalState") or (
                module.startswith("nanopynix.inproc") and alias.name in _SESSION_NAMES
            ):
                direct.add(bound)
    return frozenset(modules), frozenset(direct)


def construction_sites(source: str) -> list[tuple[int, str]]:
    """Each in-process evaluator or session construction in ``source``."""
    tree = ast.parse(source)
    modules, direct = _bound_names(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node.func)
        head, _, leaf = rendered.rpartition(".")
        if (
            rendered in direct
            or (head in modules and leaf in _SESSION_NAMES)
            or (leaf == "EvalState" and head == "nanopynix")
        ):
            found.append((node.lineno, rendered))
    return sorted(found)


def carries_the_marker(source: str) -> bool:
    """Whether the module assigns ``evaluator_in_process`` to ``pytestmark``.

    Reads the assignment as text rather than importing the module, because
    importing a test module here would run its collection-time code twice.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets)
            and "evaluator_in_process" in ast.unparse(node.value)
        ):
            return True
    return False


def fixture_names(source: str) -> set[str]:
    """Each function in ``source`` that a ``pytest.fixture`` decorator wraps."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if "pytest.fixture" in ast.unparse(decorator):
                names.add(node.name)
    return names


def test_the_scanner_can_see_the_test_tree() -> None:
    """Fail loudly if the gate is pointed somewhere with no tests in it."""
    assert TESTS_DIR.is_dir(), f"{TESTS_DIR} is missing"
    modules = [path for path in iter_python_files(TESTS_DIR) if path.name.startswith("test_")]
    assert len(modules) > 50, f"only {len(modules)} test module(s) under {TESTS_DIR}; the path is wrong"


def test_the_scanner_finds_each_construction() -> None:
    assert construction_sites("import nanopynix\nnanopynix.EvalState(store)\n") == [(2, "nanopynix.EvalState")]
    assert construction_sites("from nanopynix import inproc\ninproc.Session()\n") == [(2, "inproc.Session")]
    assert construction_sites("import nanopynix.inproc as ip\nip.Session()\n") == [(2, "ip.Session")]
    assert construction_sites("from nanopynix.inproc import EvalSession\nEvalSession()\n") == [(2, "EvalSession")]


def test_the_scanner_ignores_the_rpc_engine() -> None:
    """The rpc names collide with the inproc ones, and must not count.

    ``nanopynix.rpc.Session`` and the rpc client's ``EvalSession`` build their
    evaluator in a worker process, which is the case the rule is *for*.
    """
    assert construction_sites("import nanopynix\nnanopynix.rpc.Session()\n") == []
    assert construction_sites("from nanopynix.rpc.client import EvalSession\nEvalSession()\n") == []


def test_the_marker_reader_works_both_ways() -> None:
    assert carries_the_marker("pytestmark = pytest.mark.evaluator_in_process\n")
    assert carries_the_marker("pytestmark = [pytest.mark.concurrency, pytest.mark.evaluator_in_process]\n")
    assert not carries_the_marker("pytestmark = pytest.mark.concurrency\n")
    assert not carries_the_marker("")


def test_each_named_fixture_still_exists() -> None:
    """The first decay mode: a rename that leaves the rule matching nothing."""
    defined: set[str] = set()
    check_roster()
    for directory in FIXTURE_DIRS:
        for path in iter_python_files(directory):
            defined |= fixture_names(path.read_text(encoding="utf-8"))
    missing = sorted(IN_PROCESS_EVALUATOR_FIXTURES - defined)
    assert not missing, (
        f"IN_PROCESS_EVALUATOR_FIXTURES names {missing}, and no fixture in {FIXTURE_DIRS} is called that. "
        "The no-collector rule now finds fewer tests than it did. Point "
        "IN_PROCESS_EVALUATOR_FIXTURES in nanopynix_testing.nix_runtime at the new name."
    )


def test_every_direct_construction_is_marked_or_in_the_ledger() -> None:
    """The second decay mode: a direct construction that no fixture reveals."""
    unmarked: list[str] = []
    for path in iter_python_files(TESTS_DIR):
        if not path.name.startswith("test_"):
            continue
        relative = str(path.relative_to(REPO_ROOT))
        if relative in LEDGER:
            continue
        source = path.read_text(encoding="utf-8")
        sites = construction_sites(source)
        if sites and not carries_the_marker(source):
            unmarked.append(f"{relative}:{sites[0][0]}: {sites[0][1]}")
    assert not unmarked, (
        f"these test modules build an in-process evaluator that no fixture reveals: {unmarked}. "
        "Add `pytestmark = pytest.mark.evaluator_in_process`, so the no-collector build skips them. "
        "If the module builds a Session and never evaluates, add it to LEDGER here with that reason."
    )


def test_every_ledger_entry_still_constructs() -> None:
    """A ledger of exceptions must not outlive the code it excuses."""
    stale: list[str] = []
    for relative in sorted(LEDGER):
        path = REPO_ROOT / relative
        if not path.is_file():
            stale.append(f"{relative} (the file is gone)")
        elif not construction_sites(path.read_text(encoding="utf-8")):
            stale.append(f"{relative} (it builds no session any more)")
    assert not stale, f"delete these LEDGER entries: {stale}"


class _FakeItem:
    """The two members of ``pytest.Item`` that ``hosts_an_evaluator`` reads.

    A real ``Item`` needs a collected module behind it, and building one here
    would test pytest's collection rather than the rule. The rule reads
    ``fixturenames`` and ``get_closest_marker`` and nothing else, so those two
    are the whole contract.
    """

    def __init__(self, fixtures: tuple[str, ...] = (), marker: str | None = None) -> None:
        self.fixturenames = fixtures
        self._marker = marker

    def get_closest_marker(self, name: str) -> object | None:
        return pytest.mark.__getattr__(name) if name == self._marker else None


def test_the_rule_finds_a_test_through_its_fixtures() -> None:
    assert hosts_an_evaluator(_FakeItem(fixtures=("tmp_path", "eval_state")))  # type: ignore[arg-type] -- stand-in, see _FakeItem
    assert hosts_an_evaluator(_FakeItem(fixtures=("inproc_session",)))  # type: ignore[arg-type] -- stand-in, see _FakeItem
    assert not hosts_an_evaluator(_FakeItem(fixtures=("tmp_path", "store")))  # type: ignore[arg-type] -- stand-in, see _FakeItem


def test_the_rule_finds_a_marked_test_with_no_such_fixture() -> None:
    assert hosts_an_evaluator(_FakeItem(marker="evaluator_in_process"))  # type: ignore[arg-type] -- stand-in, see _FakeItem
    assert not hosts_an_evaluator(_FakeItem(marker="concurrency"))  # type: ignore[arg-type] -- stand-in, see _FakeItem
