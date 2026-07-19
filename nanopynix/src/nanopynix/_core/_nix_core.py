"""Shared direct-pointer Nix operations used by in-process and worker APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import store as nanopynix_store
from nanopynix_bindings import util as nanopynix_util

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


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

    def open_store(self, uri: str) -> Any:
        return nanopynix_store.open_store(uri)

    def open_eval_state(
        self,
        store: Any,
        nix_path: Sequence[str],
        build_store: Any | None = None,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> Any:
        return nanopynix_expr.EvalState(
            store,
            list(nix_path),
            build_store,
            dict(eval_settings) if eval_settings else {},
            dict(fetch_settings) if fetch_settings else {},
        )

    def configure_eval_state(
        self,
        eval_state: Any,
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
