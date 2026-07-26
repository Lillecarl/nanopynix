"""Transport-neutral local Nix resources.

These objects own direct L1 bindings and are intentionally synchronous. The
public ``inproc`` API may share a ``LocalStore`` across Store-pool threads, but
each ``LocalEvalState`` and its values remain confined to one evaluator thread.
The RPC worker places the same objects behind opaque remote handles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import flake as nanopynix_flake
from nanopynix_bindings import store as nanopynix_store

from nanopynix._core._nix_core import NixCore

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class LocalStore:
    """One direct thread-safe store pointer shared by the Store pool."""

    def __init__(self, raw: nanopynix_store.Store) -> None:
        self.raw: nanopynix_store.Store | None = raw

    def close(self) -> None:
        raw = self.raw
        self.raw = None
        if raw is not None:
            raw.close()

    def require_raw(self) -> nanopynix_store.Store:
        if self.raw is None:
            raise RuntimeError("local store has been closed")
        return self.raw

    def __getattr__(self, name: str) -> Any:
        """Forward anything not defined here to the binding, as ``Any``.

        Transitional. Store methods are being given typed definitions on this
        class, mirroring :class:`LocalValue`; until every one of them has one,
        the RPC worker's store service still reflects over the binding's
        ``store_*`` proto entrypoints by name (``_worker_store.py``'s
        ``_nanobind_rpc_call``) for the remainder. The cost is that a typo in
        an attribute name reaches this instead of the type checker, which is
        why it is going away -- prefer a typed method, or :meth:`require_raw`,
        wherever the name is known statically.
        """
        return getattr(self.require_raw(), name)

    def _store_path(self, path: str | nanopynix_store.StorePath) -> nanopynix_store.StorePath:
        """Normalise a caller-supplied path to a ``nix::StorePath``.

        This is the Python home of what ``nix_store.cpp``'s
        ``store_path_from_string()`` did for the proto-dict entrypoints, so
        that both engines share one implementation instead of inproc using the
        direct binding (no absolutization) and rpc using the dict funnel
        (absolutization). A relative path is resolved against the store
        directory, matching what the rpc engine has always accepted.

        The empty string is deliberately *not* rejected here: it is forwarded
        to ``parse_store_path``, whose C++ guard raises ``BadStorePath`` rather
        than letting ``canonPath``'s assertion abort the process. Keeping the
        rejection there means both engines keep reporting it exactly as they
        do today, and the guard stays reachable for raw-binding callers too.
        """
        if isinstance(path, nanopynix_store.StorePath):
            return path
        raw = self.require_raw()
        if path and not path.startswith("/"):
            path = f"{raw.get_store_dir()}/{path}"
        return raw.parse_store_path(path)

    def print_store_path(self, path: nanopynix_store.StorePath | str) -> str:
        """Render a store path absolute, whichever of Nix's two spellings arrives.

        The union is not convenience: the bindings genuinely return both.
        Anything that goes through ``parse_store_path`` hands back a
        ``StorePath``, whose ``str()`` is the bare ``hash-name``, while the
        collective queries funnel through C++'s ``store_paths_to_string_list``
        and hand back strings that are already absolute. Normalising both here
        is what lets one helper serve either; the prefix test is what makes it
        idempotent.
        """
        text = str(path)
        store_dir = self.require_raw().get_store_dir().rstrip("/")
        if text == store_dir or text.startswith(f"{store_dir}/"):
            return text
        return f"{store_dir}/{text}"

    def print_store_paths(self, paths: Sequence[nanopynix_store.StorePath | str]) -> list[str]:
        return [self.print_store_path(path) for path in paths]

    def is_valid_path(self, path: str | nanopynix_store.StorePath) -> bool:
        return self.require_raw().is_valid_path(self._store_path(path))

    def copy_closure(
        self,
        paths: Sequence[str | nanopynix_store.StorePath],
        dest_store: LocalStore,
        *,
        repair: bool = False,
        check_sigs: bool = True,
        substitute: bool = False,
    ) -> None:
        self.require_raw().copy_closure(
            [self._store_path(path) for path in paths],
            dest_store.require_raw(),
            repair,
            check_sigs,
            substitute,
        )


class LocalEvalState:
    """One direct evaluator pointer bound to a :class:`LocalStore`."""

    def __init__(self, raw: nanopynix_expr.EvalState, store: LocalStore) -> None:
        self.raw: nanopynix_expr.EvalState | None = raw
        self.store = store
        self._values: set[LocalValue] = set()
        self._locked_flakes: set[LocalLockedFlake] = set()

    def close(self) -> None:
        for value in tuple(self._values):
            value.close()
        for locked_flake in tuple(self._locked_flakes):
            locked_flake.close()
        self.raw = None

    def require_raw(self) -> nanopynix_expr.EvalState:
        if self.raw is None:
            raise RuntimeError("local evaluator has been closed")
        return self.raw

    def __getattr__(self, name: str) -> Any:
        """Forward to the binding as ``Any`` -- see :meth:`LocalStore.__getattr__`."""
        return getattr(self.require_raw(), name)

    def wrap_value(self, raw: nanopynix_expr.Value) -> LocalValue:
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
        flake_settings: Mapping[str, str] | None = None,
    ) -> LocalLockedFlake:
        raw = nanopynix_flake.lock_flake(
            self.require_raw(),
            nanopynix_flake.parse_flake_ref(ref),
            update_inputs=update_inputs,
            write_lock_file=write_lock_file,
            flake_settings=dict(flake_settings) if flake_settings else {},
        )
        locked_flake = LocalLockedFlake(self, raw)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    def call_locked_flake(self, locked_flake: LocalLockedFlake) -> LocalValue:
        return self.wrap_value(nanopynix_flake.call_flake(self.require_raw(), locked_flake.require_raw()))

    def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool,
        flake_settings: Mapping[str, str] | None = None,
    ) -> LocalValue:
        return self.wrap_value(
            nanopynix_flake.eval_flake(
                self.require_raw(),
                ref,
                write_lock_file,
                dict(flake_settings) if flake_settings else {},
            ),
        )

    def configure(
        self,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> None:
        """Apply live-mutable eval/fetch settings to this already-open evaluator."""
        raw = self.require_raw()
        for name, value in (eval_settings or {}).items():
            raw.set_eval_setting(name, value)
        for name, value in (fetch_settings or {}).items():
            raw.set_fetch_setting(name, value)

    def discard_locked_flake(self, locked_flake: LocalLockedFlake) -> None:
        self._locked_flakes.discard(locked_flake)


class LocalValue:
    """One rooted L1 value, confined to its owning Nix thread.

    ``nanopynix_expr.Value`` contains Nix's ``RootValue``. This wrapper gives
    both L2 and L3 one ownership and child-value construction boundary, while
    keeping the raw pointer private to thread-confined local code.
    """

    def __init__(self, eval_state: LocalEvalState, raw: nanopynix_expr.Value) -> None:
        self._eval_state = eval_state
        self._raw: nanopynix_expr.Value | None = raw

    def close(self) -> None:
        raw = self._raw
        self._raw = None
        self._eval_state.discard_value(self)
        if raw is not None:
            raw._release()  # type: ignore[reportPrivateUsage] -- L1 RootValue lifetime API  # noqa: SLF001

    def require_raw(self) -> nanopynix_expr.Value:
        self._eval_state.require_raw()
        if self._raw is None:
            raise RuntimeError("local value has been released")
        return self._raw

    def force(self) -> None:
        self.require_raw().force()

    def to_python(self) -> nanopynix_expr.ValueType:
        return self.require_raw().to_python()

    def to_json(self, copy_to_store: bool = False) -> nanopynix_expr.ValueType:
        # Keyword, not positional: the binding declares copy_to_store
        # keyword-only (nanopynix-bindings/src/expr.pat). The positional call
        # this replaces went unnoticed while `raw` was typed Any.
        return self.require_raw().to_json(copy_to_store=copy_to_store)

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

    def edit_location(self) -> nanopynix_expr.EditLocation:
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

    def build(self, build_store: LocalStore | None, build_mode: int, eval_store: LocalStore | None) -> dict[str, object]:
        return self.require_raw().build(
            None if build_store is None else build_store.require_raw(),
            build_mode,
            None if eval_store is None else eval_store.require_raw(),
        )

    def derived_path(self) -> str:
        """Return this derivation's self-contained canonical DerivedPath string."""
        return self.require_raw().derived_path()


class LocalLockedFlake:
    """One in-memory locked flake, confined to its owning Nix thread."""

    def __init__(self, eval_state: LocalEvalState, raw: nanopynix_flake.LockedFlake) -> None:
        self._eval_state = eval_state
        self._raw: nanopynix_flake.LockedFlake | None = raw

    def close(self) -> None:
        self._raw = None
        self._eval_state.discard_locked_flake(self)

    def require_raw(self) -> nanopynix_flake.LockedFlake:
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
    ) -> None:
        self._core.initialize(
            settings=settings,
            load_config=load_config,
            verbosity=verbosity,
        )

    def open_store(self, uri: str) -> LocalStore:
        return LocalStore(self._core.open_store(uri))

    def open_eval_state(
        self,
        store: LocalStore,
        nix_path: Sequence[str],
        build_store: LocalStore | None = None,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> LocalEvalState:
        return LocalEvalState(
            self._core.open_eval_state(
                store.require_raw(),
                nix_path,
                None if build_store is None else build_store.require_raw(),
                eval_settings,
                fetch_settings,
            ),
            store,
        )

    def get_verbosity(self) -> int:
        return self._core.get_verbosity()

    def set_verbosity(self, verbosity: int) -> int:
        return self._core.set_verbosity(verbosity)
