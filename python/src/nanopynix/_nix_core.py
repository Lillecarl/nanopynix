"""Shared direct-pointer Nix operations used by in-process and worker APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nanopynix_expr
import nanopynix_store
import nanopynix_util

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
        pure_eval: bool | None,
        restrict_eval: bool | None,
        allowed_uris: Sequence[str],
    ) -> None:
        for name, value in settings.items():
            nanopynix_util.set_setting(name, value)
        nanopynix_util.init_libstore(load_config=load_config)
        if verbosity is not None:
            nanopynix_util.set_verbosity(verbosity)
        nanopynix_expr.init_libexpr()
        if pure_eval is not None:
            nanopynix_expr._set_pure_eval(pure_eval)  # type: ignore[reportPrivateUsage] -- evaluator configuration binding
        if restrict_eval is not None:
            nanopynix_expr._set_restrict_eval(restrict_eval)  # type: ignore[reportPrivateUsage] -- evaluator configuration binding
        if allowed_uris:
            nanopynix_expr._set_allowed_uris(list(allowed_uris))  # type: ignore[reportPrivateUsage] -- evaluator configuration binding

    def open_store(self, uri: str) -> Any:
        return nanopynix_store.open_store(uri)

    def open_eval_state(self, store: Any, nix_path: Sequence[str]) -> Any:
        return nanopynix_expr.EvalState(store, list(nix_path))

    def get_verbosity(self) -> int:
        return nanopynix_util.get_verbosity()

    def set_verbosity(self, verbosity: int) -> int:
        nanopynix_util.set_verbosity(verbosity)
        return nanopynix_util.get_verbosity()
