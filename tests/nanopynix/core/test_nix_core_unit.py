"""Direct unit test for NixCore.configure_eval_state.

NixCore's other methods are exercised indirectly through nanopynix.inproc's
real Session/Store fixtures, but nothing currently calls
configure_eval_state -- it exists for live-mutable eval/fetch settings but has
no caller yet. This dumb coverage test pins its loop behavior directly with a
fake EvalState so it doesn't bit-rot before it gets a real caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix._core._nix_core import NixCore

if TYPE_CHECKING:
    from nanopynix_bindings.expr import EvalState

    from nanopynix._core._nix_core import EvalSettingsTarget

    def _a_real_eval_state_satisfies_the_protocol(  # type: ignore[reportUnusedFunction] -- a static assertion; pyright checking it is the entire point
        state: EvalState,
    ) -> EvalSettingsTarget:
        """Pin ``EvalSettingsTarget`` to the concrete type it abstracts.

        ``configure_eval_state`` has no production caller yet, so without this
        the Protocol's only consumer is the fake below -- and it could drift
        into a shape a real ``EvalState`` no longer satisfies without anything
        noticing. Checked by pyright; never executed.
        """
        return state


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
