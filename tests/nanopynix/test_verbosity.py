from __future__ import annotations

import pytest

from nanopynix import LogLevel, normalize_log_level
from nanopynix.rpc import Session


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, LogLevel.ERROR),
        ("0", LogLevel.ERROR),
        ("ERROR", LogLevel.ERROR),
        ("error", LogLevel.ERROR),
        ("WARN", LogLevel.WARN),
        ("warn", LogLevel.WARN),
        ("NOTICE", LogLevel.NOTICE),
        ("notice", LogLevel.NOTICE),
        ("INFO", LogLevel.INFO),
        ("info", LogLevel.INFO),
        ("TALKATIVE", LogLevel.TALKATIVE),
        ("talkative", LogLevel.TALKATIVE),
        ("CHATTY", LogLevel.CHATTY),
        ("chatty", LogLevel.CHATTY),
        ("DEBUG", LogLevel.DEBUG),
        ("debug", LogLevel.DEBUG),
        ("VOMIT", LogLevel.VOMIT),
        ("vomit", LogLevel.VOMIT),
        ("lvlVomit", LogLevel.VOMIT),
    ],
)
def test_normalize_log_level_accepts_nix_names(raw: int | str, expected: LogLevel) -> None:
    assert normalize_log_level(raw) == expected


@pytest.mark.parametrize("raw", [-1, 8, "loud", ""])
def test_normalize_log_level_rejects_invalid_values(raw: int | str) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_log_level(raw)


async def test_session_updates_live_worker_verbosity() -> None:
    async with Session() as session:
        assert await session.get_verbosity() == LogLevel.NOTICE
        assert await session.set_verbosity("debug") == LogLevel.DEBUG
        assert await session.get_verbosity() == LogLevel.DEBUG
