"""Direct unit test for NixCore.configure_eval_state.

NixCore's other methods are exercised indirectly through nanopynix.inproc's
real Session/Store fixtures, but nothing currently calls
configure_eval_state -- it exists for live-mutable eval/fetch settings but has
no caller yet. This dumb coverage test pins its loop behavior directly with a
fake EvalState so it doesn't bit-rot before it gets a real caller.
"""

from __future__ import annotations

from nanopynix._core._nix_core import NixCore


class _FakeEvalState:
    def __init__(self) -> None:
        self.eval_settings: list[tuple[str, str]] = []
        self.fetch_settings: list[tuple[str, str]] = []

    def set_eval_setting(self, name: str, value: str) -> None:
        self.eval_settings.append((name, value))

    def set_fetch_setting(self, name: str, value: str) -> None:
        self.fetch_settings.append((name, value))


def test_configure_eval_state_applies_eval_and_fetch_settings() -> None:
    core = NixCore()
    eval_state = _FakeEvalState()

    core.configure_eval_state(eval_state, eval_settings={"a": "1"}, fetch_settings={"b": "2"})

    assert eval_state.eval_settings == [("a", "1")]
    assert eval_state.fetch_settings == [("b", "2")]


def test_configure_eval_state_defaults_to_no_settings() -> None:
    core = NixCore()
    eval_state = _FakeEvalState()

    core.configure_eval_state(eval_state)

    assert eval_state.eval_settings == []
    assert eval_state.fetch_settings == []
