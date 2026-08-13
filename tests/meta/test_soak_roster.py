"""The concurrency soak keeps its reach, and its exclusions keep their reasons.

:mod:`nanopynix_testing.soak` runs the tests that already exist, concurrently, so
that ThreadSanitizer watches a wide surface instead of the few tests somebody
wrote to overlap on purpose. Its roster discovers itself, which is what makes
it grow with the suite -- and which gives it two decay modes.

**The first is a roster that quietly shrinks.** Every condition in the scanner
excludes a test, and a scanner that excludes everything reports no race just as
happily as a clean tree does. The size gate below is the floor under that.

**The second is a denylist that grows without argument.** An entry there is a
finding somebody decided not to fix, so it carries the reason, and the reason
has to still be about a test that exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanopynix_testing.soak import DENYLIST, discover_roster, roster_hash

REPO_ROOT = Path(__file__).resolve().parents[2]

# The floor, not the count. The roster is meant to grow, so this fails on the
# loss of a whole engine or a scanner condition that went too wide, and does
# not fail on the ordinary addition or removal of a test.
_MINIMUM = {"inproc": 50, "rpc": 30}


@pytest.mark.parametrize("engine", ["inproc", "rpc"])
def test_the_soak_still_reaches_a_useful_number_of_tests(engine: str) -> None:
    roster = discover_roster(root=REPO_ROOT, engine=engine)
    assert len(roster) >= _MINIMUM[engine], (
        f"the {engine} soak roster fell to {len(roster)}, below the floor of {_MINIMUM[engine]}. "
        f"A condition in nanopynix_testing.soak went too wide, or a fixture was renamed. "
        f"A soak that runs nothing reports no race exactly like a clean tree does."
    )


def test_every_denied_test_still_exists() -> None:
    """A stale entry is worse than none: it reads as a considered exclusion.

    The scanner drops a denied nodeid before it looks the function up, so a
    renamed test leaves its entry behind, silently excluding nothing.
    """
    stale: list[str] = []
    for nodeid in DENYLIST:
        path, _, name = nodeid.partition("::")
        source = REPO_ROOT / path
        if not source.is_file() or f"async def {name}(" not in source.read_text(encoding="utf-8"):
            stale.append(nodeid)
    assert not stale, "DENYLIST in nanopynix_testing.soak names tests that no longer exist:\n  " + "\n  ".join(stale)


def test_every_denied_test_says_why() -> None:
    thin = [nodeid for nodeid, reason in DENYLIST.items() if len(reason.split()) < 8]
    assert not thin, (
        "each DENYLIST entry in nanopynix_testing.soak states why the test cannot share a lane. "
        "These say too little to act on:\n  " + "\n  ".join(thin)
    )


def test_a_denied_test_is_not_also_excluded_by_the_scanner() -> None:
    """An entry the scanner would have dropped anyway is noise.

    It claims a decision that nothing rests on, and it survives the two tests
    above while meaning nothing.
    """
    reachable = {
        candidate.nodeid for engine in ("inproc", "rpc") for candidate in discover_roster(root=REPO_ROOT, engine=engine)
    }
    # Re-run discovery with the denylist emptied, so the difference is exactly
    # what the denylist removes rather than what the scanner removes.
    unfiltered = dict(DENYLIST)
    DENYLIST.clear()
    try:
        without = {
            candidate.nodeid
            for engine in ("inproc", "rpc")
            for candidate in discover_roster(root=REPO_ROOT, engine=engine)
        }
    finally:
        DENYLIST.update(unfiltered)

    redundant = sorted(set(unfiltered) - (without - reachable))
    assert not redundant, (
        "these DENYLIST entries in nanopynix_testing.soak change nothing, because a scanner "
        "condition already excludes them:\n  " + "\n  ".join(redundant)
    )


def test_a_mark_that_a_plugin_wrote_does_not_shrink_the_roster() -> None:
    """The roster must not depend on what pytest already imported.

    ``anyio_mode = auto`` makes anyio write ``usefixtures("anyio_backend")``
    onto each async test **function object** as it collects it. The scanner
    reads that same object whenever pytest's module name and its own agree,
    which is every module at the top of ``nanopynix/tests/``.

    **CI run 31710004856 lost 23 of 111 tests that way, on all twelve test
    jobs, and the floor gate above passed on 52 against a floor of 50.** The
    check below is the one that fails first, and it names the mark.

    The mark is applied here rather than waited for, so the gate holds in a
    run of this file alone, and it goes on every test of the roster rather
    than on a chosen module. Both are what anyio does in a full run, and a
    second copy of the mark changes nothing, so the tests stay as they are.
    """
    roster = discover_roster(root=REPO_ROOT)
    before = {candidate.nodeid for candidate in roster}
    assert before, "the scanner found no test at all, so this gate proves nothing"

    for candidate in roster:
        pytest.mark.usefixtures("anyio_backend")(candidate.func)

    lost = sorted(before - {candidate.nodeid for candidate in discover_roster(root=REPO_ROOT)})
    assert not lost, (
        f"the `usefixtures` mark that anyio writes onto an async test removed {len(lost)} of "
        f"{len(before)} tests from the soak roster:\n  " + "\n  ".join(lost) + "\n"
        "Name the fixture in _PLUGIN_INJECTED_FIXTURES in nanopynix_testing.soak. Until you do, "
        "the roster shrinks by whatever a plugin decides to mark, and it does so in silence."
    )


def test_the_roster_hash_is_stable_across_two_scans() -> None:
    """Replay rests on this. A roster that reorders makes a seed meaningless."""
    first = discover_roster(root=REPO_ROOT, engine="inproc")
    second = discover_roster(root=REPO_ROOT, engine="inproc")
    assert roster_hash(first) == roster_hash(second)
