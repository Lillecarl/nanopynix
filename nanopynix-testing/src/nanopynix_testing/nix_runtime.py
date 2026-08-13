"""Runtime facts and validated pytest markers for the linked Nix build."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

import nanopynix

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class NixVersion:
    """Comparable linked-Nix version, independent of package output names."""

    parts: tuple[int, ...]

    @classmethod
    def parse(cls, value: str) -> NixVersion:
        match = re.match(r"(\d+(?:\.\d+)*)", value)
        if match is None:
            raise ValueError(f"cannot parse linked Nix version: {value!r}")
        return cls(tuple(int(part) for part in match.group(1).split(".")))

    def padded(self, width: int = 3) -> tuple[int, ...]:
        return self.parts + (0,) * max(0, width - len(self.parts))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, NixVersion):
            return NotImplemented
        width = max(len(self.parts), len(other.parts))
        return self.padded(width) < other.padded(width)


@dataclass(frozen=True)
class NixRuntime:
    version: NixVersion
    version_text: str
    capabilities: frozenset[str]

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# The fixtures that give a test an evaluator inside the pytest process.
#
# pytest computes the transitive closure of the fixtures of a test into
# `item.fixturenames`, so a test that reaches one of these through another
# fixture is in this set as well, and no test module has to name the rule.
#
# `tests/meta/test_no_collector_rule.py` checks that both names still resolve
# to a fixture. A rename would otherwise leave the rule matching nothing, and
# a rule that matches nothing looks exactly like a rule with nothing to do.
IN_PROCESS_EVALUATOR_FIXTURES = frozenset({"eval_state", "inproc_session"})

# **A build with no collector must not host an evaluator in a long-lived
# process.** Nix's own package option says what `enableGC = false` does: "we
# just leak memory, but this is not as bad as it sounds so long as evaluation
# just takes place within short-lived processes". An RPC worker is such a
# process, and it returns every byte when it exits. The pytest process is not:
# it outlives the whole suite, so each evaluator it builds accumulates until
# the run dies. A measured run of the whole suite against the no-collector
# build reached 5.47 GB and the kernel killed it.
#
# The tests here are not broken. Each one passes against this build. What
# fails is the demand they make together, and the measurements against
# nix_2_34-nogc give it: the rpc share of the suite peaked at 553 MB, the
# in-process share demanded about 10 GB, and the whole suite in one process
# reached 5 GB resident with 14.6 GB of swap before the kernel killed it.
#
# **The default forks each of these tests, and that is what lets a
# no-collector build run the whole suite.** The mode adds `pytest.mark.forked`,
# so the child exits when the test ends and the operating system takes the
# memory back. That is the trade the RPC worker already makes, applied to the
# one engine that has no worker. Measured against nix_2_34-nogc: the whole
# suite passed 2077 tests at a 3 GB peak in 12 minutes, and the in-process
# subset alone fell from about 10 GB to 297 MB.
#
# A forked child that constructed its own `EvalState` used to abort, unless
# `init_libexpr` ran in the parent first. Neither abort was catchable: bdwgc
# refuses `GC_register_my_thread` before `GC_INIT`, and behind that Nix
# asserts that `initGC` ran. `PyEvalState::init` now starts the collector
# itself, so any process can build an evaluator with no ordering rule to obey.
# Issue #54, and `TestGitFetcherSettings` no longer takes an ordering fixture
# for it.
#
# Forking is not free. A forked test pays for every session-scoped fixture
# again, because a fixture built in a child dies with the child.
NO_COLLECTOR_SKIP_REASON = "the build has no collector, so an evaluator in the pytest process never releases memory"

# **Nothing here needs a flag to find out which build it is running on.**
# `build_info` publishes `boehm_gc`, the hook below reads it, and a build with
# a collector never reaches any of these modes: every test runs in the pytest
# process, as it always did. The option decides only what happens when the
# collector is absent.
#
# `fork` is that answer. `skip` is the escape hatch for a build where forking
# itself breaks, and it was the default until it did not have to be. `run` and
# `only` are for a bounded, deliberate run: `run` tells a fork failure apart
# from a collector failure, which is how issue #54 was found, and `only`
# selects the subset for a measurement.
IN_PROCESS_EVALUATOR_MODES = frozenset({"fork", "skip", "run", "only"})

NOT_IN_PROCESS_SKIP_REASON = "--in-process-evaluator=only selected the in-process tests, and this is not one"


def hosts_an_evaluator(item: pytest.Item) -> bool:
    """Whether ``item`` builds a Nix evaluator inside the pytest process.

    A test that builds one through a fixture is found by the fixture closure.
    A test that builds one directly carries ``evaluator_in_process``, because
    nothing in the fixture graph records that.
    """
    if item.get_closest_marker("evaluator_in_process") is not None:
        return True
    fixtures: object = getattr(item, "fixturenames", ())
    if not isinstance(fixtures, (tuple, list)):
        return False
    return not IN_PROCESS_EVALUATOR_FIXTURES.isdisjoint(cast("list[str]", fixtures))


def linked_nix_runtime() -> NixRuntime:
    """Read the compiled Nix facts from the extension under test."""
    info: Any = nanopynix.build_info()  # type: ignore[reportUnknownVariableType, reportUnknownMemberType] -- extension lacks stubs
    version_text = info["nix_version"]
    if not isinstance(version_text, str):
        raise TypeError(f"build_info nix_version is not a string: {version_text!r}")
    capabilities_raw = info["capabilities"]
    if not isinstance(capabilities_raw, dict):
        raise TypeError("build_info capabilities is not a mapping")
    capabilities = cast("dict[str, bool]", capabilities_raw)
    return NixRuntime(
        version=NixVersion.parse(version_text),
        version_text=version_text,
        capabilities=frozenset(name for name, enabled in capabilities.items() if enabled),
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--nix-test-backends",
        default="local",
        help="comma-separated hermetic Nix backends: local,daemon (CI and the TSan workflows always pass this "
        "explicitly, so this default only governs plain local `pytest` invocations)",
    )
    parser.addoption(
        "--nix-sanitizer",
        default=os.environ.get("NANOPYNIX_TEST_SANITIZER"),
        help="active Nix sanitizer, for example tsan",
    )
    parser.addoption(
        "--in-process-evaluator",
        default="fork",
        choices=sorted(IN_PROCESS_EVALUATOR_MODES),
        help="what to do with a test that builds an evaluator in the pytest process, on a build with no collector: "
        "fork it into a child (the default), skip it, run it in this process anyway, or run only those tests. "
        "See NO_COLLECTOR_SKIP_REASON",
    )


def pytest_configure(config: pytest.Config) -> None:
    for marker in (
        "nix_version(minimum=None, maximum=None, exclude=()): require a linked Nix version range",
        "nix_capability(name): require a compiled nanopynix/Nix capability",
        "nix_sanitizer(name): run only under the named sanitizer",
        (
            "nix_known_issue(exclude=(), sanitizer=None, reason=''): skip an explicitly bounded "
            "upstream defect. Give `exclude`, or `sanitizer`, or both"
        ),
        "evaluator_in_process: builds a Nix evaluator in the pytest process, without a fixture",
        "live_gc: test performs destructive garbage collection",
        "forks_the_process: test calls fork() itself, so it cannot join a concurrency lane",
        "concurrency: test intentionally overlaps worker, executor, session, or log operations",
        "soak: runs the existing tests concurrently, for the ThreadSanitizer workflow",
        "l3_inproc: real in-process L3 worker tests with worker-side lifecycle inspection",
    ):
        config.addinivalue_line("markers", marker)


def configured_backends(config: pytest.Config) -> tuple[str, ...]:
    raw = config.getoption("--nix-test-backends")
    if not isinstance(raw, str):
        raise pytest.UsageError("--nix-test-backends must be a comma-separated string")
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    allowed = {"local", "daemon"}
    unknown = set(values) - allowed
    if not values or unknown:
        raise pytest.UsageError(f"--nix-test-backends accepts local,daemon; got {raw!r}")
    return values


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "nix_backend" in metafunc.fixturenames:
        metafunc.parametrize("nix_backend", configured_backends(metafunc.config), scope="session")


def _version_in_exclusions(version: NixVersion, values: Iterable[object]) -> str | None:
    """Match an exclusion against a version, at the precision of the exclusion.

    `"2.34"` means the 2.34 series, so it matches 2.34.8. `"2.34.8"` means
    that release alone. Compare the leading parts, and no more.

    **This used to compare `padded()` on both sides, and `padded` only ever
    adds parts.** So `"2.34"` gave `(2, 34)` against a version of `(2, 34, 8)`
    and never matched, while a Nix built from git reports `2.35pre2026...`,
    which parses to exactly `(2, 35)` and did match `"2.35"`. Every exclusion
    written as two parts therefore did the opposite of what it says: it spared
    the git build and skipped nothing else. See the marker on
    `test_inproc_mixed_evaluation_build_and_store_workloads`, which named
    2.34 and 2.35 and ran on both.
    """
    for value in values:
        if not isinstance(value, str):
            raise pytest.UsageError("nix_version exclude entries must be version strings")
        excluded = NixVersion.parse(value)
        width = len(excluded.parts)
        if version.padded(width)[:width] == excluded.parts:
            return value
    return None


def known_issue_skip_reason(
    *,
    version: NixVersion,
    sanitizer: str | None,
    exclude: tuple[object, ...] | list[object],
    issue_sanitizer: str | None,
    reason: str,
) -> str | None:
    """The skip reason for a `nix_known_issue` marker, or None to run it.

    A pure function, so `tests/meta/test_known_issue_marker.py` can check the
    decision without driving a pytest collection. The hook below is the only
    caller, and it does the argument validation that pytest reports as a
    usage error.

    `exclude` bounds a defect by Nix version, and `issue_sanitizer` bounds it
    by build. Either alone is a bound. Both together mean the defect needs the
    two to meet, which is the narrowest claim and the one to prefer.
    """
    excluded = _version_in_exclusions(version, exclude)
    version_matches = excluded is not None if exclude else True
    sanitizer_matches = issue_sanitizer is None or issue_sanitizer == sanitizer
    if not (version_matches and sanitizer_matches):
        return None
    where = [] if excluded is None else [f"Nix {excluded}"]
    if issue_sanitizer is not None:
        where.append(f"under {issue_sanitizer}")
    return f"{reason}; {' '.join(where)}"


# `tryfirst`, and not by accident. This hook adds `pytest.mark.forked` to a
# test, and the hook of the same name in tests/conftest.py sorts on that
# marker to put every forked test at the front of the run. That order is a
# deadlock rule and not a preference: fork() keeps only the calling thread, so
# a fork after another test has started a Nix thread leaves a held lock locked
# forever in the child. Without `tryfirst` the sort would read the marker
# before this hook writes it, and every forked test would run in the wrong
# place.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: C901, PLR0912, PLR0915 -- tracked complexity/arg-count debt, see TODO.md
    runtime = linked_nix_runtime()
    sanitizer = config.getoption("--nix-sanitizer", default=None)
    has_collector = runtime.supports("boehm_gc")
    mode = config.getoption("--in-process-evaluator")
    if mode not in IN_PROCESS_EVALUATOR_MODES:
        raise pytest.UsageError(f"--in-process-evaluator accepts {sorted(IN_PROCESS_EVALUATOR_MODES)}; got {mode!r}")
    for item in items:
        in_process = hosts_an_evaluator(item)
        if mode == "only" and not in_process:
            item.add_marker(pytest.mark.skip(reason=NOT_IN_PROCESS_SKIP_REASON))
        elif in_process and not has_collector:
            if mode == "skip":
                item.add_marker(pytest.mark.skip(reason=NO_COLLECTOR_SKIP_REASON))
            elif mode == "fork":
                item.add_marker(pytest.mark.forked)

        version_marker = item.get_closest_marker("nix_version")
        if version_marker is not None:
            if version_marker.args:
                raise pytest.UsageError("nix_version accepts keyword arguments only")
            allowed = {"minimum", "maximum", "exclude", "reason"}
            unknown = set(version_marker.kwargs) - allowed
            if unknown:
                raise pytest.UsageError(f"nix_version has unknown arguments: {sorted(unknown)!r}")
            minimum = version_marker.kwargs.get("minimum")
            maximum = version_marker.kwargs.get("maximum")
            exclude = version_marker.kwargs.get("exclude", ())
            reason = version_marker.kwargs.get("reason", "linked Nix version is unsupported")
            if minimum is not None and (not isinstance(minimum, str) or runtime.version < NixVersion.parse(minimum)):
                item.add_marker(pytest.mark.skip(reason=f"{reason}; requires Nix >= {minimum}"))
            if maximum is not None and (
                not isinstance(maximum, str) or not runtime.version < NixVersion.parse(maximum)
            ):
                item.add_marker(pytest.mark.skip(reason=f"{reason}; requires Nix < {maximum}"))
            if not isinstance(exclude, (tuple, list)):
                raise pytest.UsageError("nix_version exclude must be a tuple or list")
            excluded = _version_in_exclusions(runtime.version, cast("tuple[object, ...] | list[object]", exclude))
            if excluded is not None:
                item.add_marker(pytest.mark.skip(reason=f"{reason}; unsupported on Nix {excluded}"))

        capability = item.get_closest_marker("nix_capability")
        if capability is not None:
            if capability.kwargs or len(capability.args) != 1 or not isinstance(capability.args[0], str):
                raise pytest.UsageError("nix_capability requires one capability name")
            if not runtime.supports(capability.args[0]):
                item.add_marker(pytest.mark.skip(reason=f"linked Nix lacks {capability.args[0]}"))

        sanitizer_marker = item.get_closest_marker("nix_sanitizer")
        if sanitizer_marker is not None:
            if (
                sanitizer_marker.kwargs
                or len(sanitizer_marker.args) != 1
                or not isinstance(sanitizer_marker.args[0], str)
            ):
                raise pytest.UsageError("nix_sanitizer requires one sanitizer name")
            if sanitizer != sanitizer_marker.args[0]:
                item.add_marker(pytest.mark.skip(reason=f"requires {sanitizer_marker.args[0]} sanitizer"))

        issue_marker = item.get_closest_marker("nix_known_issue")
        if issue_marker is not None:
            if issue_marker.args:
                raise pytest.UsageError("nix_known_issue accepts keyword arguments only")
            allowed = {"exclude", "sanitizer", "reason"}
            unknown = set(issue_marker.kwargs) - allowed
            if unknown:
                raise pytest.UsageError(f"nix_known_issue has unknown arguments: {sorted(unknown)!r}")
            issue_sanitizer = issue_marker.kwargs.get("sanitizer")
            excluded_versions = issue_marker.kwargs.get("exclude", ())
            reason = issue_marker.kwargs.get("reason")
            if not isinstance(reason, str) or not reason:
                raise pytest.UsageError("nix_known_issue requires a non-empty reason")
            if issue_sanitizer is not None and not isinstance(issue_sanitizer, str):
                raise pytest.UsageError("nix_known_issue sanitizer must be a string or None")
            if not isinstance(excluded_versions, (tuple, list)):
                raise pytest.UsageError("nix_known_issue exclude must be a tuple or list")
            # A defect has to be bounded by something. A marker that names
            # neither a version nor a sanitizer skips the test everywhere, and
            # that is a deletion written as a mark.
            if not excluded_versions and issue_sanitizer is None:
                raise pytest.UsageError("nix_known_issue requires exclude, or sanitizer, or both")
            skip_reason = known_issue_skip_reason(
                version=runtime.version,
                sanitizer=sanitizer,
                exclude=cast("tuple[object, ...] | list[object]", excluded_versions),
                issue_sanitizer=issue_sanitizer,
                reason=reason,
            )
            if skip_reason is not None:
                item.add_marker(pytest.mark.skip(reason=skip_reason))


@pytest.fixture(scope="session")
def nix_runtime() -> NixRuntime:
    return linked_nix_runtime()
