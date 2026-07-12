"""Unit tests for worker eval store-handle selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nanopynix._handle_registry import HandleRegistry
from nanopynix._worker_eval import EvalServiceHandler


class _FakeEvalState:
    def __init__(self, store: object, nix_path: list[str]) -> None:
        self.store = store
        self.nix_path = nix_path
        self.released: list[Any] = []

    def release_exported_value(self, value: Any) -> None:
        self.released.append(value)


def test_eval_state_uses_requested_store_handle(monkeypatch):
    import nanopynix._worker_eval as worker_eval

    monkeypatch.setattr(worker_eval.nanopynix_expr, "EvalState", _FakeEvalState)
    monkeypatch.setattr(worker_eval.nanopynix_expr, "parse_nix_path", lambda: ["nixpkgs=/tmp/nixpkgs"])

    handles = HandleRegistry()
    first_handle = handles.allocate("first-store", "store")
    second_handle = handles.allocate("second-store", "store")
    state = SimpleNamespace(
        eval_state=None,
        eval_store_handle=None,
        handles=handles,
    )
    handler = EvalServiceHandler(state)

    selected = handler._get_es(second_handle)

    assert isinstance(selected, _FakeEvalState)
    assert selected.store == "second-store"
    assert selected.nix_path == ["nixpkgs=/tmp/nixpkgs"]
    assert state.eval_store_handle == second_handle

    assert handler._get_es(second_handle) is selected

    switched = handler._get_es(first_handle)

    assert switched is not selected
    assert switched.store == "first-store"
    assert state.eval_store_handle == first_handle
