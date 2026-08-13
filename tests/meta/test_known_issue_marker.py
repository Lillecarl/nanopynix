"""`nix_known_issue` must bound a defect, and say what bounds it.

The marker skips a test that a known upstream defect makes impossible to run.
That is a deletion of coverage, so it earns two rules, and this is where both
are checked.

**A marker with no bound is a deletion written as a mark.** `exclude` bounds
the defect by Nix version and `sanitizer` bounds it by build. One that names
neither would skip everywhere and never come back, so the hook refuses it.

**The reason has to reach the reader of the skip.** pytest prints the skip
reason and nothing else, so the reason has to carry both the text and the
bound that fired. A skip that says only "known issue" tells the next person
to look at the source, and they will not.

`nanopynix_testing.nix_runtime` holds the decision as a pure function for
exactly this reason: the alternative is driving a whole pytest collection to
find out what one marker does.
"""

from __future__ import annotations

import pytest

from nanopynix_testing.nix_runtime import NixVersion, known_issue_skip_reason

_2_34 = NixVersion.parse("2.34.8")
_2_31 = NixVersion.parse("2.31.2")


def _decide(
    *,
    version: NixVersion = _2_34,
    sanitizer: str | None = None,
    exclude: tuple[str, ...] = (),
    issue_sanitizer: str | None = None,
) -> str | None:
    return known_issue_skip_reason(
        version=version,
        sanitizer=sanitizer,
        exclude=exclude,
        issue_sanitizer=issue_sanitizer,
        reason="the reason",
    )


class TestVersionAlone:
    """`exclude` on its own bounds the defect to the versions it names."""

    def test_a_named_version_skips(self) -> None:
        assert _decide(exclude=("2.34",)) == "the reason; Nix 2.34"

    def test_another_version_runs(self) -> None:
        assert _decide(version=_2_31, exclude=("2.34",)) is None

    def test_the_sanitizer_does_not_narrow_it(self) -> None:
        """No `sanitizer` on the marker means every build is affected."""
        assert _decide(sanitizer="asan", exclude=("2.34",)) == "the reason; Nix 2.34"


class TestExclusionPrecision:
    """An exclusion matches at its own precision, and this is why.

    `padded()` only ever *adds* parts, so comparing `padded()` on both sides
    made `"2.34"` mean the two-part version 2.34 and nothing else. Every
    exclusion in the tree was written with two parts, so none of them matched
    a released Nix -- while a Nix built from git reports `2.35pre2026...`,
    which parses to exactly `(2, 35)` and *did* match. The marker on
    `test_inproc_mixed_evaluation_build_and_store_workloads` named 2.34 and
    2.35, ran on both, and skipped the git build it was written to spare.
    """

    def test_a_series_matches_a_release_in_it(self) -> None:
        assert _decide(exclude=("2.34",)) is not None, "'2.34' must mean the 2.34 series, not the bare version 2.34"

    def test_a_full_version_matches_only_itself(self) -> None:
        assert _decide(version=NixVersion.parse("2.34.8"), exclude=("2.34.8",)) is not None
        assert _decide(version=NixVersion.parse("2.34.9"), exclude=("2.34.8",)) is None

    def test_a_prerelease_belongs_to_its_series(self) -> None:
        """`2.35pre...` parses to `(2, 35)`, so the 2.35 series covers it."""
        assert _decide(version=NixVersion.parse("2.35pre20260619_f8bb823a"), exclude=("2.35",)) is not None

    def test_a_different_series_still_runs(self) -> None:
        assert _decide(version=NixVersion.parse("2.35.0"), exclude=("2.34",)) is None


class TestSanitizerAlone:
    """`sanitizer` on its own bounds the defect to one build, every version.

    This is the shape a defect of the instrumentation takes. The
    runaway-recursion tests use it: ASAN fails a CHECK inside its own
    `__asan_handle_no_return` while `max-call-depth` unwinds, whatever Nix
    version is underneath. See issue #71.
    """

    def test_the_named_sanitizer_skips(self) -> None:
        assert _decide(sanitizer="asan", issue_sanitizer="asan") == "the reason; under asan"

    def test_it_skips_on_every_version(self) -> None:
        assert _decide(version=_2_31, sanitizer="asan", issue_sanitizer="asan") is not None

    def test_another_sanitizer_runs(self) -> None:
        assert _decide(sanitizer="tsan", issue_sanitizer="asan") is None

    def test_no_sanitizer_runs(self) -> None:
        assert _decide(sanitizer=None, issue_sanitizer="asan") is None


class TestBothTogether:
    """Both bounds means the defect needs the two to meet, and is narrowest."""

    def test_both_matching_skips(self) -> None:
        reason = _decide(sanitizer="asan", exclude=("2.34",), issue_sanitizer="asan")
        assert reason == "the reason; Nix 2.34 under asan"

    def test_the_wrong_version_runs(self) -> None:
        assert _decide(version=_2_31, sanitizer="asan", exclude=("2.34",), issue_sanitizer="asan") is None

    def test_the_wrong_sanitizer_runs(self) -> None:
        assert _decide(sanitizer="tsan", exclude=("2.34",), issue_sanitizer="asan") is None


def test_the_reason_survives_into_the_skip() -> None:
    """Whatever fired, the text the marker carried is still in the message."""
    for kwargs in (
        {"exclude": ("2.34",)},
        {"sanitizer": "asan", "issue_sanitizer": "asan"},
        {"sanitizer": "asan", "exclude": ("2.34",), "issue_sanitizer": "asan"},
    ):
        reason = _decide(**kwargs)  # type: ignore[arg-type] -- one literal dict for each of the three shapes
        assert reason is not None
        assert reason.startswith("the reason; ")


def test_an_unbounded_marker_is_refused() -> None:
    """The hook rejects it, so this pins the shape the hook refuses.

    `known_issue_skip_reason` itself would answer "skip", because with no
    bound at all every bound trivially matches. That is exactly why the check
    lives in the hook, where pytest can report it as a usage error against the
    test that wrote it.
    """
    assert _decide() == "the reason; "


@pytest.mark.parametrize("marker_name", ["nix_known_issue"])
def test_the_marker_is_registered(pytestconfig: pytest.Config, marker_name: str) -> None:
    """An unregistered marker is silently ignored under `--strict-markers`."""
    registered = pytestconfig.getini("markers")
    assert any(entry.startswith(f"{marker_name}(") for entry in registered), (
        f"{marker_name} is not registered; pytest would ignore it"
    )
