"""Process-title setup for the pynix CLI manager."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pynix
from pynix._impl import main as impl_main

if TYPE_CHECKING:
    import pytest


def test_main_sets_up_after_it_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The order is the point, and issue #123 is why.

    ``main`` used to install a traceback handler, name the process and
    configure logging before it parsed anything. ``--help`` and a shell
    completion both end inside the parse, so a completion callback paid for
    all three and used none of them. ``configure_logging`` alone pulled
    ``structlog``, which is 195 ms.
    """
    calls: list[tuple[str, str | None]] = []
    command = MagicMock()
    monkeypatch.setattr(impl_main, "set_manager_title", lambda name: calls.append(("title", name)))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr
    monkeypatch.setattr(impl_main, "configure_logging", lambda: calls.append(("logging", None)))

    def _record_traceback_install(**_kwargs: object) -> None:
        calls.append(("traceback", None))

    monkeypatch.setattr(impl_main.rich.traceback, "install", _record_traceback_install)

    def _record_dispatch(_parser: object, _namespace: object) -> object:
        calls.append(("parse", None))
        return command

    monkeypatch.setattr(pynix, "dispatch", _record_dispatch)
    monkeypatch.setattr(impl_main, "run", lambda _body: calls.append(("run", None)))  # type: ignore[reportUnknownLambdaType] -- lambda receives Any from setattr
    monkeypatch.setattr(sys, "argv", ["pynix", "build"])

    pynix.main()

    # The parse comes first, the set-up after it, and the body last.
    assert calls == [("parse", None), ("traceback", None), ("title", "pynix"), ("logging", None), ("run", None)]
