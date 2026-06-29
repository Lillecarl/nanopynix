"""BuildResult helpers for goal implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..serde import BuildResult, BuildResultStatus

if TYPE_CHECKING:
    from ..store_path import StorePath


type DynamicPathMap = dict[tuple["StorePath | str", ...], "StorePath"]


@dataclass
class GoalResult:
    result: BuildResult
    resolved_outputs: dict[str, StorePath] = field(default_factory=dict)
    produced_paths: set[StorePath] = field(default_factory=set)
    dynamic_paths: DynamicPathMap = field(default_factory=dict)


def failure(message: str, status: BuildResultStatus = BuildResultStatus.MISC_FAILURE) -> BuildResult:
    return BuildResult(
        status=status,
        error_msg=message,
        times_built=0,
        is_non_deterministic=0,
        start_time=0,
        stop_time=0,
        built_outputs={},
    )


def success(status: BuildResultStatus = BuildResultStatus.ALREADY_VALID) -> BuildResult:
    return BuildResult(
        status=status,
        error_msg="",
        times_built=0,
        is_non_deterministic=0,
        start_time=0,
        stop_time=0,
        built_outputs={},
    )


def result_succeeded(result: BuildResult) -> bool:
    try:
        return BuildResultStatus(result.status).is_success
    except ValueError:
        return False


def goal_failure(message: str, status: BuildResultStatus = BuildResultStatus.MISC_FAILURE) -> GoalResult:
    return GoalResult(result=failure(message, status))


def goal_success(status: BuildResultStatus = BuildResultStatus.ALREADY_VALID) -> GoalResult:
    return GoalResult(result=success(status))
