"""Transport-neutral, thread-confined local Nix resources.

These objects own direct L1 bindings and are intentionally synchronous: they
must only be used on :class:`NixThreadExecutor`'s one Nix thread.  The public
``inproc`` API schedules them asynchronously, while the RPC worker places the
same objects behind opaque remote handles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import nanopynix_flake
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
        self._values: set[LocalValue] = set()
        self._locked_flakes: set[LocalLockedFlake] = set()

    def close(self) -> None:
        for value in tuple(self._values):
            value.close()
        for locked_flake in tuple(self._locked_flakes):
            locked_flake.close()
        self.raw = None

    def require_raw(self) -> Any:
        if self.raw is None:
            raise RuntimeError("local evaluator has been closed")
        return self.raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self.require_raw(), name)

    def wrap_value(self, raw: Any) -> LocalValue:
        value = LocalValue(self, raw)
        self._values.add(value)
        return value

    def discard_value(self, value: LocalValue) -> None:
        self._values.discard(value)

    def eval_string(self, expression: str, path: str) -> LocalValue:
        return self.wrap_value(self.require_raw().eval_string(expression, path))

    def eval_file(self, path: str) -> LocalValue:
        return self.wrap_value(self.require_raw().eval_file(path))

    def repl_eval_file(self, path: str) -> LocalValue:
        return self.wrap_value(self.require_raw().repl_eval_file(path))

    def repl_eval_string(self, expression: str, path: str) -> LocalValue:
        return self.wrap_value(self.require_raw().repl_eval_string(expression, path))

    def repl_load_file(self, path: str) -> LocalValue:
        return self.wrap_value(self.require_raw().repl_load_file(path))

    def repl_process_line(self, line: str, path: str) -> LocalValue | None:
        raw = self.require_raw().repl_process_line(line, path)
        return None if raw is None else self.wrap_value(raw)

    def repl_add_attrs(self, value: LocalValue) -> list[str]:
        return self.require_raw().repl_add_attrs(value.require_raw())

    def value_from_python(self, value: Any) -> LocalValue:
        return self.wrap_value(self.require_raw().value_from_python(_unwrap_local_values(value)))

    def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str],
        write_lock_file: bool,
    ) -> LocalLockedFlake:
        raw = nanopynix_flake.lock_flake(
            self.require_raw(),
            nanopynix_flake.parse_flake_ref(ref),
            update_inputs=update_inputs,
            write_lock_file=write_lock_file,
        )
        locked_flake = LocalLockedFlake(self, raw)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    def call_locked_flake(self, locked_flake: LocalLockedFlake) -> LocalValue:
        return self.wrap_value(nanopynix_flake.call_flake(self.require_raw(), locked_flake.require_raw()))

    def eval_flake(self, ref: str, *, write_lock_file: bool) -> LocalValue:
        return self.wrap_value(nanopynix_flake.eval_flake(self.require_raw(), ref, write_lock_file))

    def discard_locked_flake(self, locked_flake: LocalLockedFlake) -> None:
        self._locked_flakes.discard(locked_flake)


class LocalValue:
    """One rooted L1 value, confined to its owning Nix thread.

    ``nanopynix_expr.Value`` contains Nix's ``RootValue``. This wrapper gives
    both L2 and L3 one ownership and child-value construction boundary, while
    keeping the raw pointer private to thread-confined local code.
    """

    def __init__(self, eval_state: LocalEvalState, raw: Any) -> None:
        self._eval_state = eval_state
        self._raw: Any = raw

    def close(self) -> None:
        raw = self._raw
        self._raw = None
        self._eval_state.discard_value(self)
        if raw is not None:
            raw._release()  # type: ignore[reportPrivateUsage] -- L1 RootValue lifetime API

    def require_raw(self) -> Any:
        self._eval_state.require_raw()
        if self._raw is None:
            raise RuntimeError("local value has been released")
        return self._raw

    def force(self) -> None:
        self.require_raw().force()

    def force_deep(self) -> None:
        self.require_raw().force_deep()

    def to_python(self) -> Any:
        return self.require_raw().to_python()

    def to_json(self, copy_to_store: bool = False) -> Any:
        return self.require_raw().to_json(copy_to_store)

    def type_name(self) -> str:
        return self.require_raw().type_name()

    def as_int(self) -> int:
        return self.require_raw().as_int()

    def as_float(self) -> float:
        return self.require_raw().as_float()

    def as_bool(self) -> bool:
        return self.require_raw().as_bool()

    def as_string(self) -> str:
        return self.require_raw().as_string()

    def realise_string(self) -> str:
        return self.require_raw().realise_string()

    def realise_argv(self) -> list[str]:
        return self.require_raw().realise_argv()

    def edit_location(self) -> dict[str, Any]:
        return self.require_raw().edit_location()

    def attr_get(self, name: str) -> LocalValue:
        return self._eval_state.wrap_value(self.require_raw().attr_get(name))

    def has_attr(self, name: str) -> bool:
        return self.require_raw().has_attr(name)

    def attr_names(self) -> list[str]:
        return self.require_raw().attr_names()

    def list_get(self, index: int) -> LocalValue:
        return self._eval_state.wrap_value(self.require_raw().list_get(index))

    def list_length(self) -> int:
        return self.require_raw().list_length()

    def auto_call(self) -> LocalValue:
        return self._eval_state.wrap_value(self.require_raw().auto_call())

    def call(self, argument: LocalValue) -> LocalValue:
        return self._eval_state.wrap_value(self.require_raw().call(argument.require_raw()))

    def build(self, build_store: LocalStore | None, build_mode: int, eval_store: LocalStore | None) -> Any:
        return self.require_raw().build(
            None if build_store is None else build_store.require_raw(),
            build_mode,
            None if eval_store is None else eval_store.require_raw(),
        )


class LocalLockedFlake:
    """One in-memory locked flake, confined to its owning Nix thread."""

    def __init__(self, eval_state: LocalEvalState, raw: Any) -> None:
        self._eval_state = eval_state
        self._raw: Any = raw

    def close(self) -> None:
        self._raw = None
        self._eval_state.discard_locked_flake(self)

    def require_raw(self) -> Any:
        self._eval_state.require_raw()
        if self._raw is None:
            raise RuntimeError("local locked flake has been released")
        return self._raw

    def write_lock_file(self) -> None:
        self.require_raw().write_lock_file()


def _unwrap_local_values(value: Any) -> Any:
    if isinstance(value, LocalValue):
        return value.require_raw()
    if isinstance(value, list):
        items = cast("list[Any]", value)
        return [_unwrap_local_values(item) for item in items]
    if isinstance(value, tuple):
        items = cast("tuple[Any, ...]", value)
        return tuple(_unwrap_local_values(item) for item in items)
    if isinstance(value, dict):
        items = cast("dict[Any, Any]", value)
        return {key: _unwrap_local_values(item) for key, item in items.items()}
    return value


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
