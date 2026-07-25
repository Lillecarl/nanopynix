"""Shared direct-pointer Nix operations used by in-process and worker APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import store as nanopynix_store
from nanopynix_bindings import util as nanopynix_util

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class EvalSettingsTarget(Protocol):
    """The two live-mutable setters :meth:`NixCore.configure_eval_state` needs.

    Structural rather than the concrete ``EvalState`` because that is the whole
    of the contract -- the method applies settings and touches nothing else --
    and stating it exactly is what lets a test substitute a recorder without a
    cast. ``nanopynix_bindings.expr.EvalState`` satisfies it as written.
    """

    def set_eval_setting(self, name: str, value: str) -> None: ...

    def set_fetch_setting(self, name: str, value: str) -> None: ...


def build_mode_value(build_mode: nanopynix_store.BuildMode | int | None) -> int:
    """Normalise the three ways a caller may name a build mode to Nix's int.

    Shared because both engines accept the same three spellings and must agree
    on what each means: the enum, the raw int Nix uses on the wire, and
    ``None`` for "normal". Previously one copy lived on rpc's ValueProxy while
    inproc simply typed the parameter ``Any`` and called ``int()`` on it, which
    accepted anything with an ``__int__`` and rejected the enum's own name.
    """
    if build_mode is None:
        return nanopynix_store.BuildMode.Normal.value
    if isinstance(build_mode, int):
        return build_mode
    return build_mode.value


class NixCore:
    """Pointer-level Nix operations that must run on a Nix thread.

    This class deliberately has no asyncio, handle registry, or protobuf
    dependency. ``nanopynix.inproc`` retains its objects directly, while the
    worker wraps the same objects in RPC handles.
    """

    def initialize(
        self,
        *,
        settings: Mapping[str, str],
        load_config: bool,
        verbosity: int | None,
    ) -> None:
        for name, value in settings.items():
            nanopynix_util.set_setting(name, value)
        nanopynix_util.init_libstore(load_config=load_config)
        if verbosity is not None:
            nanopynix_util.set_verbosity(verbosity)
        nanopynix_expr.init_libexpr()

    def open_store(self, uri: str) -> nanopynix_store.Store:
        return nanopynix_store.open_store(uri)

    def open_eval_state(
        self,
        store: nanopynix_store.Store,
        nix_path: Sequence[str],
        build_store: nanopynix_store.Store | None = None,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> nanopynix_expr.EvalState:
        return nanopynix_expr.EvalState(
            store,
            list(nix_path),
            build_store,
            dict(eval_settings) if eval_settings else {},
            dict(fetch_settings) if fetch_settings else {},
        )

    def configure_eval_state(
        self,
        eval_state: EvalSettingsTarget,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> None:
        """Apply live-mutable eval/fetch settings to an already-open ``EvalState``."""
        for name, value in (eval_settings or {}).items():
            eval_state.set_eval_setting(name, value)
        for name, value in (fetch_settings or {}).items():
            eval_state.set_fetch_setting(name, value)

    def get_verbosity(self) -> int:
        return nanopynix_util.get_verbosity()

    def set_verbosity(self, verbosity: int) -> int:
        nanopynix_util.set_verbosity(verbosity)
        return nanopynix_util.get_verbosity()
