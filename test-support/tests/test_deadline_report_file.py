"""Tests for the file that collects a hang report.

`test_support.deadline` writes each report to `NANOPYNIX_HANG_REPORT_FILE`
as well as attaching it to the exception. The file is the only copy that
survives a CI step which is killed before pytest prints its FAILURES section,
and that is the case the whole mechanism exists for.

These tests follow the rule in `tests/AGENTS.md` for a helper of the harness:
a defect here makes no other test fail, it only stops the report arriving at
the one moment a reader needs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest

from test_support.deadline import HANG_REPORT_FILE_VAR, with_test_timeout

if TYPE_CHECKING:
    from pathlib import Path


async def _hangs() -> None:
    await anyio.sleep(60)


async def _returns() -> None:
    return


async def test_a_timeout_writes_the_report_to_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is what a killed CI step leaves behind."""
    record = tmp_path / "hang.log"
    monkeypatch.setenv(HANG_REPORT_FILE_VAR, str(record))

    with pytest.raises(TimeoutError):
        await with_test_timeout(_hangs, timeout=0.01)()

    written = record.read_text(encoding="utf-8")
    assert "_hangs" in written, f"the report names no test: {written}"
    assert "hang report" in written


async def test_two_timeouts_both_reach_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job loses six tests to this, so the file appends rather than replaces."""
    record = tmp_path / "hang.log"
    monkeypatch.setenv(HANG_REPORT_FILE_VAR, str(record))

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await with_test_timeout(_hangs, timeout=0.01)()

    assert record.read_text(encoding="utf-8").count("=== hang report:") == 2


async def test_a_test_that_finishes_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / "hang.log"
    monkeypatch.setenv(HANG_REPORT_FILE_VAR, str(record))

    await with_test_timeout(_returns)()

    assert not record.exists()


async def test_the_exception_still_carries_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is an addition, and it does not replace the note."""
    monkeypatch.setenv(HANG_REPORT_FILE_VAR, str(tmp_path / "hang.log"))

    with pytest.raises(TimeoutError) as caught:
        await with_test_timeout(_hangs, timeout=0.01)()

    assert caught.value.__notes__, "the timeout carries no hang report"


async def test_an_unwritable_file_does_not_replace_the_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller must see the timeout, whatever the file system says.

    A directory is not a file a caller can append to, so this drives the
    failing branch without depending on permissions, which a root user and a
    build sandbox each answer differently.
    """
    monkeypatch.setenv(HANG_REPORT_FILE_VAR, str(tmp_path))

    with pytest.raises(TimeoutError):
        await with_test_timeout(_hangs, timeout=0.01)()


async def test_no_variable_means_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local default writes nothing at all."""
    monkeypatch.delenv(HANG_REPORT_FILE_VAR, raising=False)

    with pytest.raises(TimeoutError):
        await with_test_timeout(_hangs, timeout=0.01)()
