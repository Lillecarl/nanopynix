# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
# conftest.py interacts heavily with nanopynix/pynix C++ nanobind extensions
# that lack type stubs.  Member types, variable types, private imports,
# unknown argument types, and lambdas without type context are inherent to the
# test infrastructure.
"""What this suite needs, and the scenario objects that only it uses.

Issue #130 moved the suite here from ``tests/pynix/``, so that a Nix
invocation reads one project and not the whole repository. Everything the root
``tests/conftest.py`` used to supply is now registered below.

``nanopynix_testing.fixtures`` is present here and absent from the helpers
suite: these tests drive a real CLI against a real store, so they need the
store, the evaluator and ``repo_root``.

``support/`` is a package inside the suite because nothing outside the suite
reads it. The language-server fixtures that used to be re-exported here moved
to ``pynix-lsp/tests/conftest.py`` with the server itself, in issue #107.

Every other plugin, beartype's import hook included, is named with ``-p`` in
``pynix/pytest.ini``. That file gives the reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import io
import json
import os
import shutil
import signal
import tempfile
import uuid
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest
import structlog
from structlog.exceptions import DropEvent

import pynix._util as pynix_util
from _shared_sessions import FAITHFUL_SESSIONS_ENV_VAR, SharedSessions

# Session scope, and anyio's own `anyio_backend` is module scope. Both are
# global plugins now that `pytest.ini` registers ours with `-p`, and anyio
# wins that tie -- every session-scoped fixture that requests it then fails
# with `ScopeMismatch`. A conftest beats any plugin, so importing the fixture
# here settles it. `nanopynix/tests/conftest.py` carries the same import and
# the full account.
from nanopynix_testing.nix_environment import anyio_backend as anyio_backend
from pynix import parse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator

    from structlog.typing import EventDict, WrappedLogger

    from nanopynix_testing.nix_environment import NixTestEnvironment

_CURRENT_PYNIX_TEST: ContextVar[str] = ContextVar("_CURRENT_PYNIX_TEST", default="unknown")


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
    environment: NixTestEnvironment
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
    def store_root(self) -> Path:
        return self.environment.root

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
            cmd = parse(args)
            # Parsing may configure structlog through Pynix's public re-export.
            # Reinstall the test collector before the command starts forwarding
            # Nix logs, so JSON command output remains machine-readable.
            structlog.configure(processors=[self.live_log.capture])
            await cmd.run()
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
            },
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
        if not self.environment.relocated:
            # `NANOPYNIX_TEST_SYSTEM_STORE` opened the store of the machine, so
            # there is no temporary layout to name. What stays worth asserting
            # is the property that mode exists for: the store is not diverted,
            # so `storeDir` is the directory on disk and a build there needs no
            # chroot. The layout of that store belongs to the machine, and this
            # suite must not assert what it is.
            if dirs.get("rootDir") is not None:
                raise AssertionError(f"the store of the machine has no root directory; got {dirs.get('rootDir')!r}")
            if dirs.get("realStoreDir") != dirs.get("storeDir"):
                raise AssertionError(
                    f"expected an undiverted store, got storeDir={dirs.get('storeDir')!r} "
                    f"realStoreDir={dirs.get('realStoreDir')!r}"
                )
            return dirs
        expected = {
            "storeDir": "/nix/store",
            "rootDir": str(self.store_root),
            "stateDir": str(self.store_root / "nix" / "var" / "nix"),
            "logDir": str(self.store_root / "nix" / "var" / "log" / "nix"),
            "realStoreDir": str(self.store_root / "nix" / "store"),
            # A native daemon owns builds in its connection process, so its
            # RemoteStore does not expose a local build directory.
            "buildDir": (
                str(self.store_root / "nix" / "var" / "nix" / "builds") if self.environment.backend == "local" else None
            ),
        }
        for key, value in expected.items():
            if dirs.get(key) != value:
                raise AssertionError(f"expected store dir {key}={value!r}, got {dirs.get(key)!r}; all dirs={dirs!r}")
        uri = dirs.get("uri")
        if self.environment.backend == "local":
            uri_ok = isinstance(uri, str) and uri.startswith("local://")
        else:
            # An open unix:// store may report back as the bare "daemon"
            # shorthand (Nix's own collapse for a store whose socket matches
            # the process's default) or drop its query params on older Nix
            # versions -- see NixTestEnvironment.store_uri_matches.
            uri_ok = isinstance(uri, str) and (uri == "daemon" or uri.startswith(("daemon?", "unix://")))
        if not uri_ok:
            raise AssertionError(f"expected a {self.environment.backend} store uri, got {uri!r}; all dirs={dirs!r}")
        return dirs

    async def add_text_file(self, contents: str = "temporary-store-message\n", *, test_name: str = "unknown") -> str:
        source = self.work_root / "message.txt"
        source.write_text(contents)
        data = await self.run_pynix_json(
            ["store", "add-file", str(source), "--store", self.store_url],
            test_name=test_name,
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
        # Nonce keeps this derivation's store path unique per call. The shared
        # session-scoped store means an earlier test/fixture may have already
        # realized an identical derivation, which would make this build a
        # cache hit with no build log to assert on.
        nonce = uuid.uuid4().hex
        log_nix_file = self.work_root / "log-test.nix"
        log_nix_file.write_text(f"""
        let
          pkgs = import <nixpkgs> {{}};
        in
        pkgs.stdenvNoCC.mkDerivation {{
          pname = "pynix-log-test";
          version = "1";
          dontUnpack = true;
          NANOPYNIX_TEST_NONCE = "{nonce}";
          installPhase = ''
            echo pynix-log-line >&2
            echo log-output > "$out"
          '';
        }}
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

    async def build_nixpkgs_package(
        self,
        attrpath: str,
        *,
        test_name: str = "unknown",
        verbosity: str = "0",
    ) -> str:
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
                "--attr",
                build_attrpath,
                "--store",
                self.store_url,
                "--verbosity",
                verbosity,
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
        return self.environment.physical_path(store_path)

    def _append_log_record(self, record: dict[str, object]) -> None:
        self.live_log.append(record)


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


@pytest.fixture(scope="module", autouse=True)
async def _shared_pynix_sessions(  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    anyio_backend: str,  # noqa: ARG001 -- see below; requested for its side effect on the fixture closure
) -> AsyncIterator[None]:
    """Share one Nix session/store/evaluator across pynix commands, unless CI
    asked for the faithful per-command path -- see _shared_sessions.py beside this file.

    `anyio_backend` is unused in the body and load-bearing anyway. anyio's
    plugin only wraps an async fixture when `anyio_backend` is in the
    *requesting test's* fixture closure (`pytest_fixture_setup`:
    `if "anyio_backend" in request.fixturenames`). An async autouse fixture at
    session scope is therefore fine right up until the first test to reach it
    is a sync one, at which point pytest is handed a raw async generator and
    errors out -- and every later test in the run collapses behind it with an
    internal `assert not self._finalizers`.

    Which test comes first depends on selection, so this passed for `pytest
    tests` and failed for `pytest pynix/tests/test_repl.py`, whose first test
    is sync: exactly backwards from what makes a bug easy to find. Naming the
    fixture here pulls it into every test's closure and settles it.
    """
    if os.environ.get(FAITHFUL_SESSIONS_ENV_VAR):
        yield
        return
    shared = SharedSessions()
    with pytest.MonkeyPatch.context() as monkeypatch:
        shared.install(monkeypatch)
        try:
            yield
        finally:
            await shared.aclose()


@pytest.fixture(autouse=True)
def _capture_pynix_test_structlog(  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    request: pytest.FixtureRequest,
    pynix_live_log: PynixLiveLog,
) -> Iterator[None]:
    test_name = request.node.nodeid
    old_config = structlog.get_config()
    token = _CURRENT_PYNIX_TEST.set(test_name)
    pynix_live_log.append({"event": "pytest test start", "test": test_name})
    with _pynix_configure_logging_noop():
        structlog.configure(processors=[pynix_live_log.capture])
        try:
            yield
        finally:
            pynix_live_log.append({"event": "pytest test finish", "test": test_name})
            structlog.configure(**old_config)
            _CURRENT_PYNIX_TEST.reset(token)


@pytest.fixture(scope="module")
async def pynix_store_scenario(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    request: pytest.FixtureRequest,
    repo_root: Path,
    nixpkgs_path: str,
    pynix_live_log: PynixLiveLog,
    shared_nix_environment: NixTestEnvironment,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[PynixStoreScenario]:
    work_root = tmp_path_factory.mktemp("pynix-work")
    scenario = PynixStoreScenario(
        store_url=shared_nix_environment.store_uri,
        environment=shared_nix_environment,
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
        shutil.rmtree(work_root)


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


@contextlib.contextmanager
def _patched_environ(values: dict[str, str]) -> Generator[None]:
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
def _pynix_configure_logging_noop() -> Generator[None]:
    # One name, not two. `pynix/__init__.py` used to import `configure_logging`
    # so that `main` could call it before `parse()`; it now calls
    # `pynix._impl.main.prepare()` after the parse instead, and the package no
    # longer binds the name. Issue #123.
    old_configure_logging = pynix_util.configure_logging
    pynix_util.configure_logging = lambda *, file=None: None  # noqa: ARG005 -- matching configure_logging's real signature so callers like forward_nix_logs(log_file=...) don't break
    try:
        yield
    finally:
        pynix_util.configure_logging = old_configure_logging


@contextlib.contextmanager
def _pynix_test_context(test_name: str) -> Generator[None]:
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
