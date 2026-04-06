"""Tests for the run and run_logged helpers."""

from __future__ import annotations

from tests.conftest import run_captured, run_logged


class TestRunCaptured:
    """Test the captured-output run helper."""

    async def test_success(self):
        rc, stdout, stderr = await run_captured(["echo", "hello"])
        assert rc == 0
        assert stdout.strip() == "hello"

    async def test_failure(self):
        rc, stdout, stderr = await run_captured(["false"])
        assert rc == 1

    async def test_stderr(self):
        rc, stdout, stderr = await run_captured(["sh", "-c", "echo error >&2"])
        assert rc == 0
        assert stderr.strip() == "error"


class TestRunLogged:
    """Test the streaming run_logged helper."""

    async def test_success(self):
        rc = await run_logged(["echo", "hello"])
        assert rc == 0

    async def test_failure(self):
        rc = await run_logged(["false"])
        assert rc == 1
