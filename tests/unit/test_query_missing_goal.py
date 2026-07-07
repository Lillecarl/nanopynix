"""Unit tests for read-only QueryMissing planning goals."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from pynixd.goals.engine import GoalEngine
from pynixd.goals.query_missing import QueryMissingPlanGoal
from pynixd.drv_parser import Derivation, DrvOutput
from pynixd.serde import IsValidPathResponse, QueryMissingRequest
from pynixd.serde import DerivedPath as SerdeDerivedPath
from pynixd.store_path import StorePath
from pynixd.substitution_queue import SubstitutionAvailability

if TYPE_CHECKING:
    from pynixd.context import PynixdContext


class FakeLocalStore:
    def __init__(self, valid_paths: set[str], derivations: dict[str, Derivation] | None = None) -> None:
        self.valid_paths = valid_paths
        self.derivations = derivations or {}

    async def execute(self, request: Any, **kwargs: Any) -> IsValidPathResponse:
        del kwargs
        return IsValidPathResponse(valid=str(request.path) in self.valid_paths)

    async def read_derivation(self, drv_store_path: StorePath | str) -> Derivation | None:
        return self.derivations.get(str(drv_store_path))


class FakeSubstitutionQueue:
    def __init__(self, substitutable: dict[str, SubstitutionAvailability]) -> None:
        self.substitutable = substitutable
        self.queries: list[str] = []

    async def can_substitute(self, path: StorePath) -> SubstitutionAvailability:
        self.queries.append(str(path))
        return self.substitutable.get(str(path), SubstitutionAvailability.unavailable())


def _derived_path_set(path: str) -> set[SerdeDerivedPath]:
    derived_path: Any = SerdeDerivedPath(value=path)
    return cast("set[SerdeDerivedPath]", {derived_path})


def _derivation(output_path: str, *, is_dynamic: bool = False) -> Derivation:
    return Derivation(
        outputs=[DrvOutput(output_name="out", path=output_path, hash_algo="", hash_value="")],
        is_dynamic=is_dynamic,
    )


@pytest.mark.anyio
async def test_query_missing_reports_substitutable_opaque_path() -> None:
    path = "/nix/store/00000000000000000000000000000000-example"
    substitution_queue = FakeSubstitutionQueue(
        {
            path: SubstitutionAvailability(
                available=True,
                nar_size=123,
                download_size=123,
            )
        }
    )
    ctx = cast(
        "PynixdContext",
        SimpleNamespace(
            local_store=FakeLocalStore(valid_paths=set()),
            scheduler=SimpleNamespace(substitution_queue=substitution_queue),
        ),
    )
    request = QueryMissingRequest(derived_paths=_derived_path_set(path))

    response = await QueryMissingPlanGoal(GoalEngine(ctx), request).result()

    assert {str(path) for path in response.will_substitute} == {path}
    assert not response.unknown
    assert response.nar_size == 123
    assert response.download_size == 123
    assert substitution_queue.queries == [path]


@pytest.mark.anyio
async def test_query_missing_reports_unknown_when_no_substituter_has_path() -> None:
    path = "/nix/store/00000000000000000000000000000000-example"
    substitution_queue = FakeSubstitutionQueue({})
    ctx = cast(
        "PynixdContext",
        SimpleNamespace(
            local_store=FakeLocalStore(valid_paths=set()),
            scheduler=SimpleNamespace(substitution_queue=substitution_queue),
        ),
    )
    request = QueryMissingRequest(derived_paths=_derived_path_set(path))

    response = await QueryMissingPlanGoal(GoalEngine(ctx), request).result()

    assert {str(path) for path in response.unknown} == {path}
    assert not response.will_substitute
    assert response.nar_size == 0
    assert response.download_size == 0


@pytest.mark.anyio
async def test_query_missing_skips_valid_derivation_output() -> None:
    drv_path = "/nix/store/11111111111111111111111111111111-example.drv"
    out_path = "/nix/store/22222222222222222222222222222222-example"
    substitution_queue = FakeSubstitutionQueue({})
    ctx = cast(
        "PynixdContext",
        SimpleNamespace(
            local_store=FakeLocalStore(
                valid_paths={out_path},
                derivations={drv_path: _derivation(out_path)},
            ),
            scheduler=SimpleNamespace(substitution_queue=substitution_queue),
        ),
    )
    request = QueryMissingRequest(derived_paths=_derived_path_set(f"{drv_path}!out"))

    response = await QueryMissingPlanGoal(GoalEngine(ctx), request).result()

    assert not response.will_build
    assert not response.will_substitute
    assert not response.unknown
    assert not substitution_queue.queries


@pytest.mark.anyio
async def test_query_missing_reports_substitutable_derivation_output() -> None:
    drv_path = "/nix/store/11111111111111111111111111111111-example.drv"
    out_path = "/nix/store/22222222222222222222222222222222-example"
    substitution_queue = FakeSubstitutionQueue(
        {
            out_path: SubstitutionAvailability(available=True, nar_size=42, download_size=42),
        }
    )
    ctx = cast(
        "PynixdContext",
        SimpleNamespace(
            local_store=FakeLocalStore(
                valid_paths=set(),
                derivations={drv_path: _derivation(out_path)},
            ),
            scheduler=SimpleNamespace(substitution_queue=substitution_queue),
        ),
    )
    request = QueryMissingRequest(derived_paths=_derived_path_set(f"{drv_path}!out"))

    response = await QueryMissingPlanGoal(GoalEngine(ctx), request).result()

    assert {str(path) for path in response.will_substitute} == {out_path}
    assert not response.will_build
    assert not response.unknown
    assert response.nar_size == 42


@pytest.mark.anyio
async def test_query_missing_reports_will_build_for_missing_derivation_output() -> None:
    drv_path = "/nix/store/11111111111111111111111111111111-example.drv"
    out_path = "/nix/store/22222222222222222222222222222222-example"
    substitution_queue = FakeSubstitutionQueue({})
    ctx = cast(
        "PynixdContext",
        SimpleNamespace(
            local_store=FakeLocalStore(
                valid_paths=set(),
                derivations={drv_path: _derivation(out_path)},
            ),
            scheduler=SimpleNamespace(substitution_queue=substitution_queue),
        ),
    )
    request = QueryMissingRequest(derived_paths=_derived_path_set(f"{drv_path}!out"))

    response = await QueryMissingPlanGoal(GoalEngine(ctx), request).result()

    assert {str(path) for path in response.will_build} == {drv_path}
    assert not response.will_substitute
    assert not response.unknown


@pytest.mark.anyio
async def test_query_missing_reports_will_build_for_dynamic_derivation() -> None:
    drv_path = "/nix/store/11111111111111111111111111111111-example.drv"
    out_path = "/nix/store/22222222222222222222222222222222-example"
    substitution_queue = FakeSubstitutionQueue({})
    ctx = cast(
        "PynixdContext",
        SimpleNamespace(
            local_store=FakeLocalStore(
                valid_paths=set(),
                derivations={drv_path: _derivation(out_path, is_dynamic=True)},
            ),
            scheduler=SimpleNamespace(substitution_queue=substitution_queue),
        ),
    )
    request = QueryMissingRequest(derived_paths=_derived_path_set(f"{drv_path}!out"))

    response = await QueryMissingPlanGoal(GoalEngine(ctx), request).result()

    assert {str(path) for path in response.will_build} == {drv_path}
    assert not substitution_queue.queries
