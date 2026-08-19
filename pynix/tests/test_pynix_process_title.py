"""Process-title setup for the pynix CLI manager."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pynix
from pynix._impl import main as impl_main

if TYPE_CHECKING:
    import pytest


def test_main_sets_up_after_it_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The order is the point, and issue #123 is why.

    ``main`` used to install a traceback handler, name the process and
    configure logging before ``Pynix.parse()``. clypi answers ``--help`` and a
    shell completion inside ``parse`` and exits there, so a completion
    callback paid for all three and used none of them. ``configure_logging``
    alone pulled ``structlog``, which is 195 ms.
    """
    calls: list[tuple[str, str | None]] = []
    command = MagicMock()
    monkeypatch.setattr(impl_main, "set_manager_title", lambda name: calls.append(("title", name)))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr
    monkeypatch.setattr(impl_main, "configure_logging", lambda: calls.append(("logging", None)))

    def _record_traceback_install(**_kwargs: object) -> None:
        calls.append(("traceback", None))

    monkeypatch.setattr(impl_main.rich.traceback, "install", _record_traceback_install)
    monkeypatch.setattr(pynix.Pynix, "parse", lambda: calls.append(("parse", None)) or command)  # type: ignore[reportUnknownLambdaType] -- lambda receives Any from setattr

    pynix.main()

    assert calls == [("parse", None), ("traceback", None), ("title", "pynix"), ("logging", None)]
    command.start.assert_called_once_with()
