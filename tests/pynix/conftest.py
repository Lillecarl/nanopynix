from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import io
import json
import os
import shutil
import signal
import stat
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest
import structlog
from structlog.exceptions import DropEvent

from pynix import Pynix

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator

    from structlog.typing import EventDict, WrappedLogger

_CURRENT_PYNIX_TEST: ContextVar[str] = ContextVar("_CURRENT_PYNIX_TEST", default="unknown")
_DEFAULT_TEST_NIX_VERBOSITY = 5


@dataclass
class PynixLiveLog:
    path: Path
    stack_path: Path

    def __post_init__(self) -> None:
        self.entries_by_test: dict[str, list[dict[str, object]]] = {}

    def append(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            f.write("\n")

    def entries_for(self, test_name: str) -> list[dict[str, object]]:
        return self.entries_by_test.setdefault(test_name, [])

    def capture(self, _logger: WrappedLogger, method_name: str, event_dict: EventDict) -> NoReturn:
        test_name = _CURRENT_PYNIX_TEST.get()
        entry = dict(event_dict)
        entry["log_level"] = _structlog_method_name_to_level(method_name)
        self.entries_for(test_name).append(entry)
        self.append({"test": test_name, **entry})
        raise DropEvent


@dataclass
class PynixStoreScenario:
    store_url: str
    store_root: Path
    work_root: Path
    repo_root: Path
    nixpkgs_path: str
    live_log: PynixLiveLog
    last_stdout: str = ""
    last_stderr: str = ""
    last_logs: list[dict[str, object]] | None = None
    current_system: str | None = None
    hello_path: str | None = None
    text_path: str | None = None
    local_log_path: str | None = None
    local_log_stderr: str = ""
    nixpkgs_hello_path: str | None = None
    nixpkgs_hello_unfree_path: str | None = None
    flake_hello_path: str | None = None

    @property
    def log_path(self) -> Path:
        return self.live_log.path

    async def run_pynix(self, args: list[str], *, test_name: str = "unknown") -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        self._append_log_record({"event": "pynix command start", "test": test_name, "args": args})
        before = len(self.live_log.entries_for(test_name))
        with (
            _patched_environ({"NIX_PATH": f"nixpkgs={self.nixpkgs_path}"}),
            _pynix_configure_logging_noop(),
            _pynix_test_context(test_name),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            cmd = Pynix.parse(args)  # type: ignore[reportUnknownMemberType]  # Pynix C++ nanobind extension without full stubs
            await cmd.astart()  # type: ignore[reportUnknownMemberType]  # Pynix C++ nanobind extension without full stubs
        self.last_stdout = stdout.getvalue()
        self.last_stderr = stderr.getvalue()
        self.last_logs = [dict(entry) for entry in self.live_log.entries_for(test_name)[before:]]
        self._append_log_record(
            {
                "event": "pynix command finish",
                "test": test_name,
                "args": args,
                "stdout_bytes": len(self.last_stdout),
                "stderr_bytes": len(self.last_stderr),
            }
        )
        return self.last_stdout, self.last_stderr

    async def run_pynix_json(self, args: list[str], *, test_name: str = "unknown") -> object:
        stdout, _stderr = await self.run_pynix(args, test_name=test_name)
        return json.loads(stdout)

    async def get_store_dirs(self, *, test_name: str = "unknown") -> dict[str, object]:
        data = await self.run_pynix_json(["store", "dirs", "--store", self.store_url], test_name=test_name)
        if not isinstance(data, dict):
            raise TypeError("store dirs must produce an object")
        return data

    async def assert_temp_store_dirs(self, *, test_name: str = "unknown") -> dict[str, object]:
        dirs = await self.get_store_dirs(test_name=test_name)
        expected = {
            "storeDir": "/nix/store",
            "rootDir": str(self.store_root),
            "stateDir": str(self.store_root / "nix" / "var" / "nix"),
            "logDir": str(self.store_root / "nix" / "var" / "log" / "nix"),
            "realStoreDir": str(self.store_root / "nix" / "store"),
            "buildDir": str(self.store_root / "nix" / "var" / "nix" / "builds"),
        }
        for key, value in expected.items():
            if dirs.get(key) != value:
                raise AssertionError(f"expected store dir {key}={value!r}, got {dirs.get(key)!r}; all dirs={dirs!r}")
        uri = dirs.get("uri")
        if not isinstance(uri, str) or "local" not in uri:
            raise AssertionError(f"expected local store uri, got {uri!r}; all dirs={dirs!r}")
        return dirs

    async def add_text_file(self, contents: str = "temporary-store-message\n", *, test_name: str = "unknown") -> str:
        source = self.work_root / "message.txt"
        source.write_text(contents)
        data = await self.run_pynix_json(
            ["store", "add-file", str(source), "--store", self.store_url], test_name=test_name
        )
        if not isinstance(data, dict):
            raise TypeError("store add-file must produce an object")
        self.text_path = _require_str(data, "path")
        return self.text_path

    async def get_current_system(self, *, test_name: str = "unknown") -> str:
        data = await self.run_pynix_json(["config", "current-system"], test_name=test_name)
        if not isinstance(data, dict):
            raise TypeError("config current-system must produce an object")
        self.current_system = _require_str(data, "currentSystem")
        return self.current_system

    async def build_hello(self, *, test_name: str = "unknown") -> str:
        source = self.work_root / "hello-source"
        bin_dir = source / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        hello = bin_dir / "hello"
        hello.write_text("#!/bin/sh\necho hello\n")
        hello.chmod(0o755)
        data = await self.run_pynix_json(
            [
                "store",
                "add-path",
                str(source),
                "--name",
                "pynix-scenario-hello",
                "--store",
                self.store_url,
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("store add-path must produce an object")
        self.hello_path = _require_str(data, "path")
        return self.hello_path

    async def build_local_log_derivation(self, *, test_name: str = "unknown") -> str:
        log_nix_file = self.work_root / "log-test.nix"
        log_nix_file.write_text("""
        let
          pkgs = import <nixpkgs> {};
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "pynix-log-test";
          version = "1";
          dontUnpack = true;
          installPhase = ''
            echo pynix-log-line >&2
            echo log-output > "$out"
          '';
        }
        """)
        data = await self.run_pynix_json(
            [
                "build",
                "--file",
                str(log_nix_file),
                "--store",
                self.store_url,
                "--verbosity",
                "6",
                "--print-build-logs",
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("build must produce an object")
        self.local_log_path = _require_output(data, "out")
        self.local_log_stderr = self.last_stderr
        return self.local_log_path

    async def build_nixpkgs_package(self, attrpath: str, *, test_name: str = "unknown") -> str:
        if attrpath == "hello-unfree":
            pkgs_nix_file = self.work_root / "pkgs-allow-unfree-default.nix"
            pkgs_nix_file.write_text("""
            { pkgs ? import <nixpkgs> { config.allowUnfree = true; } }:
            pkgs
            """)
            build_attrpath = attrpath
        else:
            pkgs_nix_file = self.repo_root / "default.nix"
            build_attrpath = f"pkgs.{attrpath}"
        data = await self.run_pynix_json(
            [
                "build",
                "--file",
                str(pkgs_nix_file),
                "--attrpath",
                build_attrpath,
                "--store",
                self.store_url,
                "--verbosity",
                "0",
                "--print-build-logs",
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("build must produce an object")
        output = _require_output(data, "out")
        if attrpath == "hello":
            self.nixpkgs_hello_path = output
        elif attrpath == "hello-unfree":
            self.nixpkgs_hello_unfree_path = output
        return output

    async def build_flake_hello(self, *, test_name: str = "unknown") -> str:
        system = await self.get_current_system(test_name=f"{test_name}:current-system")
        flake_ref = f"{self.repo_root}#legacyPackages.{system}.hello"
        data = await self.run_pynix_json(
            [
                "build",
                "--flake",
                flake_ref,
                "--store",
                self.store_url,
                "--verbosity",
                "0",
                "--print-build-logs",
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("build must produce an object")
        self.flake_hello_path = _require_output(data, "out")
        return self.flake_hello_path

    def require_hello_path(self) -> str:
        return _require_present(self.hello_path, "hello_path")

    def require_text_path(self) -> str:
        return _require_present(self.text_path, "text_path")

    def require_local_log_path(self) -> str:
        return _require_present(self.local_log_path, "local_log_path")

    def require_nixpkgs_hello_path(self) -> str:
        return _require_present(self.nixpkgs_hello_path, "nixpkgs_hello_path")

    def require_nixpkgs_hello_unfree_path(self) -> str:
        return _require_present(self.nixpkgs_hello_unfree_path, "nixpkgs_hello_unfree_path")

    def require_flake_hello_path(self) -> str:
        return _require_present(self.flake_hello_path, "flake_hello_path")

    def physical_path(self, store_path: str) -> Path:
        return self.store_root / store_path.removeprefix("/")

    def _append_log_record(self, record: dict[str, object]) -> None:
        self.live_log.append(record)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def pynix_live_log(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PynixLiveLog]:
    log_dir = tmp_path_factory.mktemp("pynix-live-logs")
    stack_file: io.TextIOWrapper = (log_dir / "pynix-manager-stacks.log").open("a")
    with contextlib.suppress(RuntimeError, ValueError):
        faulthandler.register(signal.SIGUSR2, file=stack_file, all_threads=True)
    log = PynixLiveLog(path=log_dir / "pynix-structlog.jsonl", stack_path=log_dir / "pynix-manager-stacks.log")
    terminal = request.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line("")
        terminal.write_line("================================================================")
        terminal.write_line(f"PYNIX LIVE STRUCTLOG JSONL: {log.path}")
        terminal.write_line(f"tail -f {log.path}")
        terminal.write_line(f"PYNIX MANAGER PID: {os.getpid()}")
        terminal.write_line(f"PYNIX MANAGER STACKS: {log.stack_path}")
        terminal.write_line(f"kill -USR2 {os.getpid()}")
        terminal.write_line("================================================================")
    yield log
    with contextlib.suppress(RuntimeError, ValueError):
        faulthandler.unregister(signal.SIGUSR2)
    stack_file.close()


@pytest.fixture(autouse=True)
def _capture_pynix_test_structlog(  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    request: pytest.FixtureRequest,
    pynix_live_log: PynixLiveLog,
) -> Iterator[None]:
    test_name = request.node.nodeid
    marker = request.node.get_closest_marker("pynix_verbosity")
    verbosity = marker.args[0] if marker is not None else _DEFAULT_TEST_NIX_VERBOSITY
    old_config = structlog.get_config()
    token = _CURRENT_PYNIX_TEST.set(test_name)
    pynix_live_log.append({"event": "pytest test start", "test": test_name})
    with _pynix_configure_logging_noop(), _nanopynix_default_verbosity(verbosity):
        structlog.configure(processors=[pynix_live_log.capture])
        try:
            yield
        finally:
            pynix_live_log.append({"event": "pytest test finish", "test": test_name})
            structlog.configure(**old_config)
            _CURRENT_PYNIX_TEST.reset(token)


@pytest.fixture(scope="module")
async def nixpkgs_path(repo_root: Path) -> str:
    stdout = await _run("nix", "eval", "--raw", "--file", str(repo_root), "pkgs.path")
    return stdout.decode().strip()


@pytest.fixture(scope="module")
async def pynix_store_scenario(
    request: pytest.FixtureRequest,
    repo_root: Path,
    nixpkgs_path: str,
    pynix_live_log: PynixLiveLog,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[PynixStoreScenario]:
    if not request.config.getoption("--run-temp-store-builds"):
        pytest.skip("requires --run-temp-store-builds")
    store_root = tmp_path_factory.mktemp("pynix-store")
    work_root = tmp_path_factory.mktemp("pynix-work")
    scenario = PynixStoreScenario(
        store_url=f"local?root={store_root}",
        store_root=store_root,
        work_root=work_root,
        repo_root=repo_root,
        nixpkgs_path=nixpkgs_path,
        live_log=pynix_live_log,
    )
    terminal = request.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line(f"pynix scenario structlog: {scenario.log_path}")
    try:
        yield scenario
    finally:
        _rmtree_force(store_root)
        _rmtree_force(work_root)


@pytest.fixture(scope="module")
async def populated_store(pynix_store_scenario: PynixStoreScenario) -> dict[str, str]:
    scenario = pynix_store_scenario
    if scenario.hello_path is None:
        await scenario.build_hello(test_name="populated_store:build_hello")
    if scenario.text_path is None:
        await scenario.add_text_file(test_name="populated_store:add_text_file")
    if scenario.local_log_path is None:
        await scenario.build_local_log_derivation(test_name="populated_store:build_local_log_derivation")
    return {
        "store_url": scenario.store_url,
        "hello_path": scenario.require_hello_path(),
        "text_path": scenario.require_text_path(),
        "log_path": scenario.require_local_log_path(),
    }


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
async def git_flake(nixpkgs_path: str) -> AsyncIterator[Path]:
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        (flake_dir / "flake.nix").write_text(f"""
        {{
          inputs.nixpkgs.url = "path:{nixpkgs_path}";
          outputs = {{ nixpkgs, ... }}:
          let
            system = builtins.currentSystem;
            pkgs = nixpkgs.legacyPackages.${{system}};
          in
          {{
            hello = pkgs.stdenvNoCC.mkDerivation {{
              pname = "test-hello";
              version = "1";
              dontUnpack = true;
              installPhase = ''
                echo hi > "$out"
              '';
            }};
            greeting = "hi";
          }};
        }}
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


@contextlib.contextmanager
def _patched_environ(values: dict[str, str]) -> Generator[None, None, None]:
    old_values: dict[str, str | None] = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@contextlib.contextmanager
def _pynix_configure_logging_noop() -> Generator[None, None, None]:
    import pynix._util as pynix_util

    old_configure_logging = pynix_util.configure_logging  # type: ignore[reportUnknownMemberType]  # pynix._util is C++/nanobind extension without stubs
    pynix_util.configure_logging = lambda: None  # type: ignore[reportUnknownMemberType, reportUnknownVariableType]  # pynix._util has no stubs
    try:
        yield
    finally:
        pynix_util.configure_logging = old_configure_logging  # type: ignore[reportUnknownMemberType]  # pynix._util has no stubs


@contextlib.contextmanager
def _nanopynix_default_verbosity(verbosity: int) -> Generator[None, None, None]:
    import nanopynix

    old_session = nanopynix.Session  # type: ignore[reportUnknownMemberType]  # nanopynix C++/nanobind extension without full stubs

    class VerboseSession(old_session):  # type: ignore[reportUnknownVariableType, reportUnknownBaseType]  # old_session type unknown from C++ extension
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs.setdefault("verbosity", verbosity)
            super().__init__(*args, **kwargs)  # type: ignore[reportUnknownMemberType]  # super() base is C++ nanobind type, no stubs

    nanopynix.Session = VerboseSession  # type: ignore[reportUnknownMemberType]  # nanopynix has no stubs
    try:
        yield
    finally:
        nanopynix.Session = old_session  # type: ignore[reportUnknownMemberType]  # nanopynix has no stubs


@contextlib.contextmanager
def _pynix_test_context(test_name: str) -> Generator[None, None, None]:
    token = _CURRENT_PYNIX_TEST.set(test_name)
    try:
        yield
    finally:
        _CURRENT_PYNIX_TEST.reset(token)


def _structlog_method_name_to_level(method_name: str) -> str:
    if method_name == "exception":
        return "error"
    if method_name == "warn":
        return "warning"
    return method_name


def _require_str(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _require_output(data: dict[str, object], output_name: str) -> str:
    outputs = data["outputs"]
    if not isinstance(outputs, dict):
        raise TypeError("outputs must be an object")
    value = outputs[output_name]
    if not isinstance(value, str):
        raise TypeError(f"outputs.{output_name} must be a string")
    return value


def _require_present(value: str | None, field_name: str) -> str:
    if value is None:
        raise AssertionError(f"scenario field {field_name} was not populated by its dependency")
    return value


def _rmtree_force(path: Path) -> None:
    def _chmod_force(p: Path, mode: int) -> None:
        with contextlib.suppress(FileNotFoundError):
            p.chmod(mode)

    def onexc(function: Callable[..., object], exc_path_str: str, _excinfo: BaseException) -> None:
        exc_path = Path(exc_path_str)
        try:
            parent = exc_path.parent
            if str(parent) != ".":
                parent.chmod(stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
            exc_path.chmod(stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
            function(exc_path)
        except FileNotFoundError:
            pass

    for root_str, dirs, files in os.walk(path):
        root = Path(root_str)
        for name in dirs:
            child = root / name
            if not child.is_symlink():
                _chmod_force(child, stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
        for name in files:
            child = root / name
            if not child.is_symlink():
                _chmod_force(child, stat.S_IWUSR | stat.S_IREAD)
        _chmod_force(root, stat.S_IWUSR | stat.S_IREAD | stat.S_IEXEC)
    shutil.rmtree(path, ignore_errors=False, onexc=onexc)
