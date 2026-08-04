"""``nix::verbosity`` is written once, and the source is where that is checked.

ThreadSanitizer found a data race on that global. It is a plain non-atomic
``Verbosity`` (``nix/util/logging.hh``), every ``debug()`` and ``printInfo()``
call site reads it on its own thread through the ``printMsg`` macro, and
``set_verbosity`` used to write it from whichever Nix thread served the call.

The fix removes the mutation rather than synchronising it. ``nix_util.cpp``
writes the global once, in ``nanopynix_bind_util``, which runs at module
import on the main thread before any Nix thread exists. Thread creation then
gives every later read a happens-before edge to that write.

**A second write anywhere gives the race back**, whatever value it writes, and
that is why this test reads the source rather than the running process.
``tests/nanopynix/test_verbosity.py`` covers the runtime half: it asserts that
``get_log_ceiling()`` does not move while a session changes its verbosity. A
write that lands before any test runs, or one that rewrites the same value, is
invisible to that assertion and plain to this one.

The self-checks at the end pin both directions. A scanner that matched nothing
would leave this file green forever while enforcing nothing -- the same
reasoning as ``tests/meta/test_suppression_grammar.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Where the single permitted write lives. Anything else is a defect.
_PINNING_FILE = Path("nanopynix-bindings/src/nix_util.cpp")

#: An assignment to the global, in any of the spellings a writer would use.
#: ``nix::verbosity`` and a bare ``verbosity`` inside ``namespace nix``, both
#: with any amount of space around the ``=``, and not ``==``.
_WRITE = re.compile(r"\b(?:nix::)?verbosity\s*=(?!=)")

_SOURCE_ROOT = Path("nanopynix-bindings/src")


def _writes_in(text: str) -> list[str]:
    """Return each line of ``text`` that assigns to the global.

    A line that only mentions the name in prose is not a write, so comment
    lines come out first. The C++ in this directory uses ``//`` throughout.
    """
    return [line for line in text.splitlines() if not line.lstrip().startswith("//") and _WRITE.search(line)]


def _sources() -> list[Path]:
    return sorted(path for path in _SOURCE_ROOT.rglob("*") if path.suffix in {".cpp", ".hh"})


def test_only_one_source_file_writes_the_verbosity_global() -> None:
    offenders = [path for path in _sources() if path != _PINNING_FILE and _writes_in(path.read_text())]
    assert not offenders, f"these files write nix::verbosity, which reintroduces the data race: {offenders}"


def test_the_pinning_file_writes_the_global_exactly_once() -> None:
    writes = _writes_in(_PINNING_FILE.read_text())
    assert len(writes) == 1, (
        f"{_PINNING_FILE} must write nix::verbosity once, and it writes it {len(writes)} times: {writes}"
    )


def test_the_one_write_is_the_pin_inside_the_module_initialiser() -> None:
    """The write must be in ``nanopynix_bind_util``, not somewhere later.

    Position is the whole argument. A write anywhere else runs after a Nix
    thread may already exist, and then it races however few times it happens.
    """
    text = _PINNING_FILE.read_text()
    initialiser = text.index("void nanopynix_bind_util(")
    write = _WRITE.search(text[initialiser:])
    assert write is not None, "nanopynix_bind_util no longer pins nix::verbosity"

    body_start = initialiser + write.start()
    # Nothing but the module docstring assignment and the pin's own comment
    # may come between the function's opening brace and the pin.
    preamble = text[initialiser:body_start]
    assert preamble.count("m.def(") == 0, "the pin must run before nanopynix_bind_util registers anything"


@pytest.mark.parametrize(
    "line",
    [
        "    nix::verbosity = nix::lvlVomit;",
        "verbosity=lvlDebug;",
        "  nix::verbosity   =   (nix::Verbosity) lvl;",
    ],
)
def test_the_scanner_sees_a_write(line: str) -> None:
    assert _writes_in(line) == [line]


@pytest.mark.parametrize(
    "line",
    [
        "    if (lvl > nix::verbosity) return;",
        "    return (int) nix::verbosity;",
        "    if (nix::verbosity == nix::lvlVomit) {",
        "// nix::verbosity = something, in prose",
        "    auto copy = nix::verbosity;",
    ],
)
def test_the_scanner_ignores_a_read_and_a_comment(line: str) -> None:
    assert _writes_in(line) == []


def test_the_scanner_runs_over_more_than_one_file() -> None:
    """A glob that matched nothing would make every test above vacuous."""
    sources = _sources()
    assert len(sources) > 5, f"the C++ scan found only {len(sources)} files"
    assert _PINNING_FILE in sources
