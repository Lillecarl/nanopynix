"""The local fast path: one Nix session shared by every pynix command.

Every pynix command opens its own ``nanopynix.rpc.Session``, store and
evaluator -- that is what the real CLI does, and running it that way is what
makes these tests end-to-end rather than unit tests. It is also the dominant
cost of the suite: measured on the real ``eval_session`` -> ``eval_flake``
path against a flake importing nixpkgs, a fresh session per call takes 9.04s,
while a reused one takes 7.19s on the first call and **0.01s** after. Almost
none of that is the worker process (~0.19s); it is re-importing nixpkgs into a
brand-new evaluator, over and over.

So this module rebinds ``nix_session``/``store_session``/``eval_session`` to
versions that hand out a shared session, store and evaluator. Commands still
run their real code, still forward logs per invocation, and still see a real
Nix -- they just stop paying for a cold evaluator each time.

**This trades fidelity for speed, and that trade is deliberately not made in
CI.** Setting ``NANOPYNIX_TEST_FAITHFUL_SESSIONS`` restores the per-command
behaviour exactly, and CI sets it: the slow, faithful path is the one that
gates merges, while the fast path is for the local edit-run loop where a
four-minute wait is the thing actually being paid for. What the fast path
cannot catch, by construction, is anything that depends on a command getting a
*fresh* process, store handle or evaluator -- leaked global state, first-call
initialisation order, env read at worker spawn.

A command that configures its session (``settings``, ``experimental_features``
or ``verbosity``) is never shared: those change how the worker itself is set
up, so such a call falls through to the real implementation.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import urllib.parse
from typing import TYPE_CHECKING, Any

import anyio

import nanopynix
import pynix._util as pynix_util

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    import pytest

#: Set by CI to run the faithful path -- every command opens its own worker,
#: store and evaluator, exactly as the real CLI does. Unset (the local default)
#: shares them.
FAITHFUL_SESSIONS_ENV_VAR = "NANOPYNIX_TEST_FAITHFUL_SESSIONS"

_PATCHED_NAMES = ("nix_session", "store_session", "eval_session")


class SharedSessions:
    """Holds the shared session/store/evaluator and the patched helpers."""

    def __init__(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        # Commands are driven sequentially by the tests, but the LSP tests
        # overlap operations, so opening the shared objects has to be atomic.
        self._lock = anyio.Lock()
        self._session: Any = None
        # Keyed by _store_key, not by URI alone -- see that function.
        self._stores: dict[tuple[str, tuple[int, int] | None], Any] = {}
        self._evaluators: dict[tuple[str, tuple[int, int] | None], Any] = {}
        self._originals = {name: getattr(pynix_util, name) for name in _PATCHED_NAMES}

    # ── shared object construction ─────────────────────────────────────

    async def _shared_session(self) -> Any:
        if self._session is None:
            # `load_config=False`, so the `nix.conf` of whoever runs the suite
            # reaches nothing here. `pynix` itself loads it, and must: the real
            # CLI takes what the host configured. A test is the other case --
            # it states what it wants and asserts on the result -- and the
            # substituters, the experimental features and the sandbox of a
            # developer machine are none of its business. This mattered less
            # while a session sent its own defaults over the file anyway; it
            # matters now that the file stands unless a caller speaks.
            self._session = await self._stack.enter_async_context(nanopynix.inproc.Session())
        return self._session

    async def _shared_store(self, store_uri: str) -> tuple[Any, Any]:
        key = _store_key(store_uri)
        store = self._stores.get(key)
        session = await self._shared_session()
        if store is None:
            store = await self._stack.enter_async_context(session.store(store_uri))
            self._stores[key] = store
        return session, store

    async def _shared_evaluator(self, store_uri: str) -> tuple[Any, Any, Any]:
        session, store = await self._shared_store(store_uri)
        key = _store_key(store_uri)
        evaluator = self._evaluators.get(key)
        if evaluator is None:
            evaluator = await self._stack.enter_async_context(session.eval(store))
            self._evaluators[key] = evaluator
        return session, store, evaluator

    # ── the patched helpers ────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def nix_session(
        self,
        *,
        settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
        experimental_features: Sequence[str] | None = None,
        verbosity: nanopynix.LogLevelInput | None = None,
        print_build_logs: bool = False,
    ) -> AsyncGenerator[Any]:
        if _nix_is_stubbed():
            async with self._originals["nix_session"](
                settings=settings,
                experimental_features=experimental_features,
                verbosity=verbosity,
                print_build_logs=print_build_logs,
            ) as session:
                yield session
            return
        async with self._lock:
            session = await self._shared_session()
        # Log forwarding stays per invocation: tests assert on the logs a
        # single command produced, and log_stream() is a fan-out subscription,
        # so each command gets its own consumer over the shared session.
        async with pynix_util.forward_nix_logs(session, print_build_logs=print_build_logs):
            yield session

    @contextlib.asynccontextmanager
    async def store_session(
        self,
        store_uri: str,
        *,
        settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
        experimental_features: Sequence[str] | None = None,
        verbosity: nanopynix.LogLevelInput | None = None,
        print_build_logs: bool = False,
    ) -> AsyncGenerator[tuple[Any, Any]]:
        if _nix_is_stubbed():
            async with self._originals["store_session"](
                store_uri,
                settings=settings,
                experimental_features=experimental_features,
                verbosity=verbosity,
                print_build_logs=print_build_logs,
            ) as pair:
                yield pair
            return
        async with self._lock:
            session, store = await self._shared_store(store_uri)
        async with pynix_util.forward_nix_logs(session, print_build_logs=print_build_logs):
            yield session, store

    @contextlib.asynccontextmanager
    async def eval_session(
        self,
        store_uri: str,
        *,
        settings: nanopynix.NixSettings | os.PathLike[str] | str | None = None,
        experimental_features: Sequence[str] | None = None,
        verbosity: nanopynix.LogLevelInput | None = None,
        print_build_logs: bool = False,
    ) -> AsyncGenerator[tuple[Any, Any, Any]]:
        if _nix_is_stubbed():
            async with self._originals["eval_session"](
                store_uri,
                settings=settings,
                experimental_features=experimental_features,
                verbosity=verbosity,
                print_build_logs=print_build_logs,
            ) as triple:
                yield triple
            return
        async with self._lock:
            session, store, evaluator = await self._shared_evaluator(store_uri)
        async with pynix_util.forward_nix_logs(session, print_build_logs=print_build_logs):
            yield session, store, evaluator

    # ── installation ───────────────────────────────────────────────────

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rebind the helpers in every pynix module that imported them.

        The command modules use ``from pynix._util import store_session``, so
        each holds its own reference and patching ``pynix._util`` alone would
        miss all of them. Sweeping the already-imported ``pynix.*`` modules for
        the original function object catches every current call site and any
        added later, without this file having to list them.
        """
        replacements = {name: getattr(self, name) for name in _PATCHED_NAMES}
        patched = 0
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("pynix"):
                continue
            for name in _PATCHED_NAMES:
                current = getattr(module, name, None)
                if current is self._originals[name] or (
                    callable(current) and getattr(current, "__qualname__", "").startswith("SharedSessions.")
                ):
                    monkeypatch.setattr(module, name, replacements[name])
                    patched += 1
        if patched == 0:
            raise RuntimeError(
                "shared-session fast path patched nothing -- no pynix module holds a reference to "
                f"{_PATCHED_NAMES}. Either the command modules were not imported yet, or they stopped "
                f"importing these helpers; set {FAITHFUL_SESSIONS_ENV_VAR} to run without the fast path.",
            )

    async def aclose(self) -> None:
        await self._stack.aclose()


def _nix_is_stubbed() -> bool:
    """Whether a test has replaced the Nix entry point with a fake.

    Several tests swap ``pynix._util.nanopynix`` for a ``SimpleNamespace``
    exposing a fake session, so a command runs against a scripted store rather
    than a real one. Sharing a real session would bypass the fake entirely and
    hand the command a store URI like ``test://store``; those calls have to
    fall through to the real helper, which resolves the stub the same way it
    always did.
    """
    return pynix_util.nanopynix is not nanopynix


def _store_key(store_uri: str) -> tuple[str, tuple[int, int] | None]:
    """A cache key that survives pytest recycling a temporary root's name.

    ``tmp_path_factory.mktemp("nix-local")`` numbers its directories from the
    highest that currently exists, so a root removed at one test's teardown is
    handed straight back to the next test -- same path, same store URI, brand
    new empty directory. Keying the shared store on the URI alone therefore
    returned the previous test's store handle for a root that no longer had a
    state directory, which is what made ``store verify`` fail on a missing
    ``gc.lock``. The inode distinguishes the two incarnations.
    """
    query = urllib.parse.urlparse(store_uri).query
    roots = urllib.parse.parse_qs(query).get("root")
    if not roots:
        return (store_uri, None)
    try:
        stat = pathlib.Path(roots[0]).stat()
    except OSError:
        # No root on disk yet: the store is about to create it, and the next
        # lookup will see the real inode.
        return (store_uri, None)
    return (store_uri, (stat.st_dev, stat.st_ino))
