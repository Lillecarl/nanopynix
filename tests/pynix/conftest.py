from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-temp-store-builds",
        action="store_true",
        default=False,
        help="run tests that build into temporary Nix stores",
    )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
async def nixpkgs_path(repo_root: Path) -> str:
    stdout = await _run("nix", "eval", "--raw", "--file", str(repo_root), "pkgs.path")
    return stdout.decode().strip()


@pytest.fixture(scope="module")
async def populated_store(
    request: pytest.FixtureRequest,
    repo_root: Path,
    nixpkgs_path: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[dict[str, str]]:
    if not request.config.getoption("--run-temp-store-builds"):
        pytest.skip("requires --run-temp-store-builds")
    store_root = tmp_path_factory.mktemp("pynix-store")
    store_url = f"local?root={store_root}"
    env = os.environ.copy()
    env["NIX_PATH"] = f"nixpkgs={nixpkgs_path}"
    stdout = await _run(
        "nix",
        "build",
        "--no-link",
        "--print-out-paths",
        "--store",
        store_url,
        "--file",
        str(repo_root),
        "pkgs.hello",
        env=env,
    )
    hello_path = stdout.decode().strip().splitlines()[-1]
    try:
        yield {"store_url": store_url, "hello_path": hello_path}
    finally:
        _rmtree_force(store_root)


@pytest.fixture
async def empty_store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[dict[str, Path | str]]:
    if not request.config.getoption("--run-temp-store-builds"):
        pytest.skip("requires --run-temp-store-builds")
    store_root = tmp_path / "store-root"
    try:
        yield {"store_url": f"local?root={store_root}", "store_root": store_root}
    finally:
        if store_root.exists():
            _rmtree_force(store_root)


@pytest.fixture
async def git_flake():
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        (flake_dir / "flake.nix").write_text("""
        {
          outputs = { ... }: {
            hello = builtins.derivation {
              name = "test-hello";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "echo hi > $out" ];
            };
            greeting = "hi";
          };
        }
        """)
        for args in (
            ["git", "init"],
            ["git", "add", "flake.nix"],
            ["git", "commit", "-m", "init"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=flake_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        yield flake_dir


async def _run(*args: str, env: dict[str, str] | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed with exit code {proc.returncode}\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )
    return stdout


def _rmtree_force(path: Path) -> None:
    def onexc(function, exc_path, excinfo) -> None:
        try:
            parent = os.path.dirname(exc_path)
            if parent:
                os.chmod(parent, stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
            os.chmod(exc_path, stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
            function(exc_path)
        except FileNotFoundError:
            pass

    for root, dirs, files in os.walk(path):
        for name in dirs:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                with contextlib.suppress(FileNotFoundError):
                    os.chmod(child, stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
        for name in files:
            child = os.path.join(root, name)
            if not os.path.islink(child):
                with contextlib.suppress(FileNotFoundError):
                    os.chmod(child, stat.S_IWUSR | stat.S_IREAD)
        with contextlib.suppress(FileNotFoundError):
            os.chmod(root, stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
    shutil.rmtree(path, ignore_errors=False, onexc=onexc)
