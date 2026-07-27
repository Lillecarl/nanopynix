"""Direct unit tests for pynix.target's evaluation-target helpers.

Real CLI integration tests (test_build.py, test_eval.py, test_flake_show.py)
only ever exercise evaluate_target/select_attr on their respective happy
paths. These dumb coverage tests pin down the auto_call_file=False branch,
the attrpath-with-empty-component guard, and evaluate_target's
already-validate()-guarded "no target" branch (kept as defense in depth, so
still worth pinning even though it's unreachable through the public
validate()-first call path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pynix.target import EvaluationTarget, EvaluationTargetError, evaluate_target, select_attr

from nanopynix.rpc import EvalSession, ValueProxy


class _FakeValue(ValueProxy):
    def __init__(self, attrs: dict[str, _FakeValue] | None = None, *, auto_called: bool = False) -> None:
        self._attrs = attrs or {}
        self.auto_called = auto_called

    async def has_attr(self, name: str) -> bool:
        return name in self._attrs

    async def attr_names(self) -> list[str]:
        return list(self._attrs)

    def attr(self, name: str) -> _FakeValue:
        return self._attrs[name]

    async def auto_call(self) -> _FakeValue:
        return _FakeValue(self._attrs, auto_called=True)


class _FakeSession(EvalSession):
    def __init__(self, file_value: _FakeValue | None = None) -> None:
        self._file_value = file_value

    async def file(self, _path: str) -> _FakeValue:
        if self._file_value is None:
            raise AssertionError("file() not expected")
        return self._file_value

    async def eval_flake(self, _ref: str) -> _FakeValue:
        raise AssertionError("eval_flake() not expected")


async def test_evaluate_target_with_a_file_skips_auto_call_by_default() -> None:
    target = EvaluationTarget(file=Path("x.nix"), attr=None, flake=None)
    value = _FakeValue()
    session = _FakeSession(file_value=value)

    result = await evaluate_target(target, session)

    assert result is value
    assert not value.auto_called


async def test_evaluate_target_raises_if_flake_is_missing_after_bypassing_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate(required=True) always runs first in practice and would
    already raise "either --file or --flake is required" before this branch;
    bypass it (on the frozen dataclass's class, since instances can't be
    patched) to pin the defensive fallback directly."""

    def _no_op_validate(_self: EvaluationTarget, *, required: bool = False) -> None:
        del required

    target = EvaluationTarget(file=None, attr=None, flake=None)
    monkeypatch.setattr(EvaluationTarget, "validate", _no_op_validate)

    with pytest.raises(EvaluationTargetError, match="either --file or --flake is required"):
        await evaluate_target(target, _FakeSession())


async def test_select_attr_rejects_an_empty_attrpath_component() -> None:
    value: Any = _FakeValue({"a": _FakeValue()})

    with pytest.raises(EvaluationTargetError, match="empty component"):
        await select_attr(value, "a..b")
