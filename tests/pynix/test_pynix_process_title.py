"""Process-title setup for the pynix CLI manager."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pynix

if TYPE_CHECKING:
    import pytest


def test_main_identifies_pynix_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []
    command = MagicMock()
    monkeypatch.setattr(pynix, "set_manager_title", lambda name: calls.append(("title", name)))  # type: ignore[reportUnknownLambdaType, reportUnknownArgumentType] -- lambda receives Any from setattr
    monkeypatch.setattr(pynix, "configure_logging", lambda: calls.append(("logging", None)))
    monkeypatch.setattr(pynix.Pynix, "parse", lambda: command)

    pynix.main()

    assert calls == [("title", "pynix"), ("logging", None)]
    command.start.assert_called_once_with()
