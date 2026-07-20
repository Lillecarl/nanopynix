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
        default="local,daemon",
        help="comma-separated hermetic Nix backends: local,daemon",
    )
    parser.addoption(
        "--nix-sanitizer",
        default=os.environ.get("NANOPYNIX_TEST_SANITIZER"),
        help="active Nix sanitizer, for example tsan",
    )


def pytest_configure(config: pytest.Config) -> None:
    for marker in (
        "nix_version(minimum=None, maximum=None, exclude=()): require a linked Nix version range",
        "nix_capability(name): require a compiled nanopynix/Nix capability",
        "nix_sanitizer(name): run only under the named sanitizer",
        "nix_known_issue(exclude=(), sanitizer=None, reason=''): skip an explicitly bounded upstream defect",
        "live_gc: test performs destructive garbage collection",
        "concurrency: test intentionally overlaps worker, executor, session, or log operations",
        "tsan_stress: concurrency test repeated by the ThreadSanitizer workflow",
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
    for value in values:
        if not isinstance(value, str):
            raise pytest.UsageError("nix_version exclude entries must be version strings")
        excluded = NixVersion.parse(value)
        if version.padded(len(excluded.parts)) == excluded.padded(len(excluded.parts)):
            return value
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    runtime = linked_nix_runtime()
    sanitizer = config.getoption("--nix-sanitizer", default=None)
    for item in items:
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
            if maximum is not None and (not isinstance(maximum, str) or not runtime.version < NixVersion.parse(maximum)):
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
            if sanitizer_marker.kwargs or len(sanitizer_marker.args) != 1 or not isinstance(sanitizer_marker.args[0], str):
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
            excluded = _version_in_exclusions(
                runtime.version,
                cast("tuple[object, ...] | list[object]", excluded_versions),
            )
            if excluded is not None and (issue_sanitizer is None or issue_sanitizer == sanitizer):
                scope = f" under {issue_sanitizer}" if issue_sanitizer is not None else ""
                item.add_marker(pytest.mark.skip(reason=f"{reason}; Nix {excluded}{scope}"))


@pytest.fixture(scope="session")
def nix_runtime() -> NixRuntime:
    return linked_nix_runtime()
