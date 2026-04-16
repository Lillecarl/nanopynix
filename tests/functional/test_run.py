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

    async def test_nix_config(self):
        rc, stdout, stderr, _ = await run_subproc(
            ["sh", "-c", 'echo "$NIX_CONFIG"'],
            nix_config={"foo": "bar", "baz": "qux"},
        )
        assert "foo = bar" in stdout
        assert "baz = qux" in stdout
        assert (
            "substituters = https://cache.nixos.org unix:///nix/var/nix/daemon-socket/socket?root=/"
            in stdout
        )

    async def test_nix_config_override(self):
        rc, stdout, stderr, _ = await run_subproc(
            ["sh", "-c", 'echo "$NIX_CONFIG"'],
            nix_config={"substituters": "https://example.org"},
        )
        assert "substituters = https://example.org" in stdout
        assert (
            "substituters = https://cache.nixos.org unix:///nix/var/nix/daemon-socket/socket?root=/"
            not in stdout
        )

    async def test_nix_config_merge(self):
        rc, stdout, stderr, _ = await run_subproc(
            ["sh", "-c", 'echo "$NIX_CONFIG"'],
            env={"NIX_CONFIG": "existing = true"},
            nix_config={"foo": "bar"},
        )
        assert "existing = true" in stdout
        assert "foo = bar" in stdout
        assert (
            "substituters = https://cache.nixos.org unix:///nix/var/nix/daemon-socket/socket?root=/"
            in stdout
        )
