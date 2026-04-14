"""Tests for the run_subproc helper."""

from __future__ import annotations

from tests.conftest import run_subproc


class TestRunSubproc:
    """Test the run_subproc helper."""

    async def test_success(self):
        rc, stdout, stderr, _ = await run_subproc(["echo", "hello"])
        assert stdout.strip() == "hello"

    async def test_failure(self):
        rc, stdout, stderr, _ = await run_subproc(["false"], expected_retcode=1)

    async def test_stderr(self):
        rc, stdout, stderr, _ = await run_subproc(["sh", "-c", "echo error >&2"])
        assert stderr.strip() == "error"
