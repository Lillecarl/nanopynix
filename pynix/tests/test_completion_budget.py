"""The budget a completion runs under, and the one variable that sets it.

`pynix._attr_completion` reads :data:`~pynix._attr_completion.BUDGET_VARIABLE`
once, at import, and not through the settings model of `pynix`. That is a
decision and not an oversight: a completion runs on every keypress that ends
in Tab, and a settings tree costs the start that the budget exists to protect.
Issue #226 measured the whole command at 0.639 s.

**The variable said "named here so a test can set it", and no test did.** So
the contract the documentation states -- a number, from the environment, with
a default -- was never checked. `docs/pynix/configuration.md` states it now,
and this module is what keeps that page true.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

import pynix._attr_completion as attr_completion

if TYPE_CHECKING:
    from collections.abc import Iterator

#: What a caller gets when the variable is unset.
#:
#: **5.0 s, and it was 2.0 s.** The old figure sat under the case it existed
#: for: issue #231 measured a flake whose input never answers, and the call
#: returned at 4.086 s. A budget that the case overruns reports nothing about
#: that case.
DEFAULT_BUDGET_SECONDS = 5.0


@pytest.fixture
def reimported() -> Iterator[None]:
    """Leave the module as this suite found it.

    Reading the environment at import means a test has to reimport to observe
    a change, and a reimport that stayed would hand the next test whatever
    this one set.
    """
    yield
    importlib.reload(attr_completion)


def test_the_budget_has_a_default(monkeypatch: pytest.MonkeyPatch, reimported: None) -> None:  # noqa: ARG001 -- the fixture restores the module
    monkeypatch.delenv(attr_completion.BUDGET_VARIABLE, raising=False)
    reloaded = importlib.reload(attr_completion)
    assert reloaded.BUDGET_SECONDS == DEFAULT_BUDGET_SECONDS


def test_the_variable_sets_the_budget(monkeypatch: pytest.MonkeyPatch, reimported: None) -> None:  # noqa: ARG001 -- see above
    monkeypatch.setenv(attr_completion.BUDGET_VARIABLE, "12.5")
    reloaded = importlib.reload(attr_completion)
    assert reloaded.BUDGET_SECONDS == 12.5


def test_the_variable_is_the_one_the_documentation_names() -> None:
    """`docs/pynix/configuration.md` names this string, so a rename breaks it."""
    assert attr_completion.BUDGET_VARIABLE == "PYNIX_COMPLETION_BUDGET"
    assert attr_completion.DEBUG_VARIABLE == "PYNIX_COMPLETION_DEBUG"
