"""Unit tests for worker eval store-handle selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nanopynix._handle_registry import HandleRegistry
from nanopynix._worker_eval import EvalServiceHandler


class _FakeEvalState:
    def __init__(self, store: object, nix_path: list[str]) -> None:
        self.store = store
        self.nix_path = nix_path
        self.released: list[Any] = []

    def release_exported_value(self, value: Any) -> None:
        self.released.append(value)


def test_eval_state_binds_to_first_requested_store_handle(monkeypatch):
    import nanopynix._worker_eval as worker_eval

    monkeypatch.setattr(worker_eval.nanopynix_expr, "EvalState", _FakeEvalState)

    handles = HandleRegistry()
    second_handle = handles.allocate("second-store", "store")
    first_handle = handles.allocate("first-store", "store")
    state = SimpleNamespace(
        eval_state=None,
        eval_store_handle=None,
        handles=handles,
        nix_path=["nixpkgs=/tmp/nixpkgs"],
    )
    handler = EvalServiceHandler(state)

    selected = handler._get_es(second_handle)

    assert isinstance(selected, _FakeEvalState)
    assert selected.store == "second-store"
    assert selected.nix_path == ["nixpkgs=/tmp/nixpkgs"]
    assert state.eval_store_handle == second_handle

    assert handler._get_es(second_handle) is selected

    with pytest.raises(RuntimeError, match="already bound to a different store"):
        handler._get_es(first_handle)

    assert state.eval_state is selected
    assert state.eval_store_handle == second_handle
