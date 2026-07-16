"""Transport-neutral, thread-confined local Nix resources.

These objects own direct L1 bindings and are intentionally synchronous: they
must only be used on :class:`NixThreadExecutor`'s one Nix thread.  The public
``inproc`` API schedules them asynchronously, while the RPC worker places the
same objects behind opaque remote handles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanopynix._nix_core import NixCore

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class LocalStore:
    """One direct store pointer, confined to the Nix thread."""

    def __init__(self, raw: Any) -> None:
        self.raw: Any = raw

    def close(self) -> None:
        self.raw = None

    def require_raw(self) -> Any:
        if self.raw is None:
            raise RuntimeError("local store has been closed")
        return self.raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self.require_raw(), name)


class LocalEvalState:
    """One direct evaluator pointer bound to a :class:`LocalStore`."""

    def __init__(self, raw: Any, store: LocalStore) -> None:
        self.raw: Any = raw
        self.store = store

    def close(self) -> None:
        self.raw = None

    def require_raw(self) -> Any:
        if self.raw is None:
            raise RuntimeError("local evaluator has been closed")
        return self.raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self.require_raw(), name)


class LocalRuntime:
    """Common synchronous Nix runtime used by L2 and L3 on the Nix thread."""

    def __init__(self) -> None:
        self._core = NixCore()

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
        self._core.initialize(
            settings=settings,
            load_config=load_config,
            verbosity=verbosity,
            pure_eval=pure_eval,
            restrict_eval=restrict_eval,
            allowed_uris=allowed_uris,
        )

    def open_store(self, uri: str) -> LocalStore:
        return LocalStore(self._core.open_store(uri))

    def open_eval_state(self, store: LocalStore, nix_path: Sequence[str]) -> LocalEvalState:
        return LocalEvalState(self._core.open_eval_state(store.require_raw(), nix_path), store)

    def get_verbosity(self) -> int:
        return self._core.get_verbosity()

    def set_verbosity(self, verbosity: int) -> int:
        return self._core.set_verbosity(verbosity)
