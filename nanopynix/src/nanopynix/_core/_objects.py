"""Transport-neutral Nix resources shared by both engines.

These objects own L1 bindings and are intentionally synchronous. The public
``inproc`` API may share a ``CoreStore`` across Store-pool threads, but each
``CoreEvalState`` and its values remain confined to one evaluator thread. The
RPC worker places the same objects behind opaque remote handles.

``Core`` and not ``Local``, which is what these were called until this module
was renamed from ``_local.py``. ``nix::LocalStore`` is a *specific* Nix store
implementation -- ``nix_store.cpp``'s ``store_get_store_dirs_direct`` does a
``dynamic_cast<nix::LocalStore *>`` precisely because an arbitrary
``nix::Store`` may not be one. ``CoreStore`` wraps whatever store it is
handed, including a ``unix://`` daemon store (which the ``daemon`` test
backend uses throughout), so the old name asserted a vtable guarantee this
layer does not make and has never exposed. ``Core`` instead names the thing
that is actually true of them: they are the ``_core`` layer both engines
build on, below the process boundary that distinguishes ``inproc`` from
``rpc``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanopynix_bindings import expr as nanopynix_expr, flake as nanopynix_flake, store as nanopynix_store
from nanopynix_proto.nix.store import GcAction, StoreDirs

from nanopynix._core._extract import flake_ref_attrs
from nanopynix._core._nix_core import NixCore
from nanopynix._wire import DEFAULT_CA_METHOD, DEFAULT_HASH_ALGO, NO_GC_LIMIT
from nanopynix.models import (
    BuildResult,
    Derivation,
    DerivationOutput,
    DerivationOutputs,
    FlakeRef,
    GcResult,
    GcRoot,
    MissingInfo,
    PathInfo,
    StorePath,
)

_RAW_GC_ACTIONS = {
    GcAction.RETURN_LIVE: nanopynix_store.GCAction.ReturnLive,
    GcAction.RETURN_DEAD: nanopynix_store.GCAction.ReturnDead,
    GcAction.DELETE_DEAD: nanopynix_store.GCAction.DeleteDead,
    GcAction.DELETE_SPECIFIC: nanopynix_store.GCAction.DeleteSpecific,
}

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class CoreStore:
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

    def query_missing(self, derived_paths: Sequence[str | nanopynix_store.StorePath]) -> MissingInfo:
        """Return which of ``derived_paths`` still need building or substituting.

        Derived paths, not store paths: a plain ``.drv`` means all of that
        derivation's outputs, and Nix's ``^`` separator selects specific ones.
        Both spellings are parsed in C++ by ``parse_derived_paths``.
        """
        result = self.require_raw().query_missing([str(path) for path in derived_paths])
        return MissingInfo(**result)

    def build_paths_with_results(
        self,
        derived_paths: Sequence[str | nanopynix_store.StorePath],
        *,
        build_mode: int,
        eval_store: CoreStore | None = None,
    ) -> list[BuildResult]:
        """Build ``derived_paths`` and return one result per Nix build outcome."""
        results = self.require_raw().build_paths_with_results(
            [str(path) for path in derived_paths],
            build_mode,
            None if eval_store is None else eval_store.require_raw(),
        )
        return [BuildResult(**result) for result in results]

    def copy_closure(
        self,
        paths: Sequence[str | nanopynix_store.StorePath],
        dest_store: CoreStore,
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

    # --- Identity ---------------------------------------------------------

    def get_uri(self, *, with_params: bool = False) -> str:
        return self.require_raw().get_uri(with_params=with_params)

    def get_store_dir(self) -> str:
        return self.require_raw().get_store_dir()

    def get_store_dirs(self) -> StoreDirs:
        return StoreDirs(**self.require_raw().get_store_dirs())

    def parse_store_path(self, path: str) -> StorePath:
        return StorePath(self.print_store_path(self._store_path(path)))

    def follow_links_to_store_path(self, path: str) -> StorePath:
        return StorePath(self.print_store_path(self.require_raw().follow_links_to_store_path(path)))

    def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        raw = self.require_raw().query_path_from_hash_part(hash_part)
        return None if raw is None else StorePath(self.print_store_path(raw))

    # --- Queries ----------------------------------------------------------

    def query_path_info(self, path: str | nanopynix_store.StorePath) -> PathInfo:
        return PathInfo(**self.require_raw().query_path_info(self._store_path(path)))

    def query_all_valid_paths(self) -> list[StorePath]:
        return self._public_paths(self.require_raw().query_all_valid_paths())

    def compute_fs_closure(
        self,
        path: str | nanopynix_store.StorePath,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        return self._public_paths(
            self.require_raw().compute_fs_closure(
                self._store_path(path),
                flip_direction,
                include_outputs,
                include_derivers,
            ),
        )

    def query_derivation_outputs(self, path: str | nanopynix_store.StorePath) -> list[StorePath]:
        return self._public_paths(self.require_raw().query_derivation_outputs(self._store_path(path)))

    def query_valid_derivers(self, path: str | nanopynix_store.StorePath) -> list[StorePath]:
        return self._public_paths(self.require_raw().query_valid_derivers(self._store_path(path)))

    def query_referrers(self, path: str | nanopynix_store.StorePath) -> list[StorePath]:
        return self._public_paths(self.require_raw().query_referrers(self._store_path(path)))

    def query_substitutable_paths(self, paths: Sequence[str | nanopynix_store.StorePath]) -> list[StorePath]:
        return self._public_paths(
            self.require_raw().query_substitutable_paths([self._store_path(path) for path in paths]),
        )

    def get_build_log(self, path: str | nanopynix_store.StorePath) -> str | None:
        return self.require_raw().get_build_log(self._store_path(path))

    def read_derivation(self, drv_path: str | nanopynix_store.StorePath) -> Derivation:
        result = self.require_raw().read_derivation(self._store_path(drv_path))
        # The two nested maps are built explicitly rather than left to pydantic's
        # dict->model coercion: the coercion works, but it is invisible to the
        # type checker, so `Derivation(**result)` would typecheck against any
        # nested shape at all -- including a wrong one.
        return Derivation(
            name=result["name"],
            system=result["system"],
            builder=result["builder"],
            args=result["args"],
            env=result["env"],
            input_srcs=result["input_srcs"],
            input_drvs={path: DerivationOutputs(**node) for path, node in result["input_drvs"].items()},
            outputs={name: DerivationOutput(**output) for name, output in result["outputs"].items()},
        )

    # --- Mutation ---------------------------------------------------------

    def ensure_path(self, path: str | nanopynix_store.StorePath) -> None:
        self.require_raw().ensure_path(self._store_path(path))

    def add_to_store(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        return StorePath(
            self.print_store_path(self.require_raw().add_to_store(path, name, method, hash_algo)),
        )

    def compute_store_path(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        return StorePath(
            self.print_store_path(self.require_raw().compute_store_path(path, name, method, hash_algo)),
        )

    def optimise_store(self) -> None:
        self.require_raw().optimise_store()

    def verify_store(self, *, check_contents: bool = False, repair: bool = False) -> bool:
        return self.require_raw().verify_store(check_contents, repair)

    # --- Garbage collection -----------------------------------------------

    def add_temp_root(self, path: str | nanopynix_store.StorePath) -> None:
        self.require_raw().add_temp_root(self._store_path(path))

    def add_perm_root(self, path: str | nanopynix_store.StorePath, gc_root: str) -> str:
        return self.require_raw().add_perm_root(self._store_path(path), gc_root)

    def add_indirect_root(self, path: str) -> None:
        """``path`` is a filesystem symlink, not a store path -- no normalisation."""
        self.require_raw().add_indirect_root(path)

    def find_roots(self, *, censor: bool = False) -> list[GcRoot]:
        return [GcRoot(link=root["link"], path=root["path"]) for root in self.require_raw().find_roots(censor)]

    def collect_garbage(
        self,
        action: GcAction,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: Sequence[str | nanopynix_store.StorePath] = (),
        max_freed: int = NO_GC_LIMIT,
    ) -> GcResult:
        try:
            raw_action = _RAW_GC_ACTIONS[action]
        except KeyError as exc:
            raise ValueError(f"unsupported garbage-collection action: {action!r}") from exc
        result = self.require_raw().collect_garbage(
            raw_action,
            ignore_liveness,
            [self._store_path(path) for path in paths_to_delete],
            max_freed,
        )
        return GcResult(paths=self._public_paths(result["paths"]), bytes_freed=result["bytes_freed"])

    def _public_paths(self, raw_paths: Sequence[nanopynix_store.StorePath | str]) -> list[StorePath]:
        return [StorePath(path) for path in self.print_store_paths(raw_paths)]


class CoreEvalState:
    """One direct evaluator pointer bound to a :class:`CoreStore`."""

    def __init__(self, raw: nanopynix_expr.EvalState, store: CoreStore) -> None:
        self.raw: nanopynix_expr.EvalState | None = raw
        self.store = store
        self._values: set[CoreValue] = set()
        self._locked_flakes: set[CoreLockedFlake] = set()

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
        """Forward any unlisted name to the L1 binding, untyped.

        The equivalent on ``CoreStore`` is gone -- every store call it used to
        absorb now has a typed method -- and this one is on the same path. It
        is what pyright cannot check about the evaluator, so it is worth
        keeping visible rather than quietly relying on.
        """
        return getattr(self.require_raw(), name)

    def wrap_value(self, raw: nanopynix_expr.Value) -> CoreValue:
        value = CoreValue(self, raw)
        self._values.add(value)
        return value

    def discard_value(self, value: CoreValue) -> None:
        self._values.discard(value)

    def eval_string(self, expression: str, path: str) -> CoreValue:
        return self.wrap_value(self.require_raw().eval_string(expression, path))

    def eval_file(self, path: str) -> CoreValue:
        return self.wrap_value(self.require_raw().eval_file(path))

    def repl_eval_file(self, path: str) -> CoreValue:
        return self.wrap_value(self.require_raw().repl_eval_file(path))

    def repl_eval_string(self, expression: str, path: str) -> CoreValue:
        return self.wrap_value(self.require_raw().repl_eval_string(expression, path))

    def repl_load_file(self, path: str) -> CoreValue:
        return self.wrap_value(self.require_raw().repl_load_file(path))

    def repl_process_line(self, line: str, path: str) -> CoreValue | None:
        raw = self.require_raw().repl_process_line(line, path)
        return None if raw is None else self.wrap_value(raw)

    def repl_add_attrs(self, value: CoreValue) -> list[str]:
        return self.require_raw().repl_add_attrs(value.require_raw())

    def value_from_python(self, value: Any) -> CoreValue:
        return self.wrap_value(self.require_raw().value_from_python(_unwrap_local_values(value)))

    def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str],
        write_lock_file: bool,
        flake_settings: Mapping[str, str] | None = None,
    ) -> CoreLockedFlake:
        raw = nanopynix_flake.lock_flake(
            self.require_raw(),
            nanopynix_flake.parse_flake_ref(ref),
            update_inputs=update_inputs,
            write_lock_file=write_lock_file,
            flake_settings=dict(flake_settings) if flake_settings else {},
        )
        locked_flake = CoreLockedFlake(self, raw)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    def call_locked_flake(self, locked_flake: CoreLockedFlake) -> CoreValue:
        return self.wrap_value(nanopynix_flake.call_flake(self.require_raw(), locked_flake.require_raw()))

    def get_flake(self, ref: str) -> FlakeRef:
        """Resolve a flake reference without evaluating its outputs.

        Shared rather than left in the RPC worker, where the three steps below
        used to live inline: this is pure libexpr and a registry lookup, so
        there was nothing about it that belonged to a transport. inproc had no
        equivalent at all, which the signature ledger carried as
        "EvalSession.get_flake:rpc-only".
        """
        resolved = nanopynix_flake.get_flake(
            self.require_raw(),
            nanopynix_flake.parse_flake_ref(ref),
        )
        return FlakeRef(attrs=flake_ref_attrs(resolved))

    def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool,
        flake_settings: Mapping[str, str] | None = None,
    ) -> CoreValue:
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

    def discard_locked_flake(self, locked_flake: CoreLockedFlake) -> None:
        self._locked_flakes.discard(locked_flake)


class CoreValue:
    """One rooted L1 value, confined to its owning Nix thread.

    ``nanopynix_expr.Value`` contains Nix's ``RootValue``. This wrapper gives
    both L2 and L3 one ownership and child-value construction boundary, while
    keeping the raw pointer private to thread-confined local code.
    """

    def __init__(self, eval_state: CoreEvalState, raw: nanopynix_expr.Value) -> None:
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

    def attr_get(self, name: str) -> CoreValue:
        return self._eval_state.wrap_value(self.require_raw().attr_get(name))

    def has_attr(self, name: str) -> bool:
        return self.require_raw().has_attr(name)

    def attr_names(self) -> list[str]:
        return self.require_raw().attr_names()

    def list_get(self, index: int) -> CoreValue:
        return self._eval_state.wrap_value(self.require_raw().list_get(index))

    def list_length(self) -> int:
        return self.require_raw().list_length()

    def auto_call(self) -> CoreValue:
        return self._eval_state.wrap_value(self.require_raw().auto_call())

    def call(self, *arguments: CoreValue) -> CoreValue:
        """Apply this value as a Nix function to each argument in turn.

        Nix functions are curried -- ``f a b`` is ``(f a) b`` -- so more than
        one argument means more than one application, and the partial results
        in between are rooted values no caller ever sees. They are released
        here, including on the way out of a failure, because nothing else
        holds a reference to them.

        Shared rather than per-engine: inproc took exactly one ``argument``,
        which the signature ledger carried as ``Value.call:params``, and the
        RPC worker ran this loop inline in ``_do_call``. That copy leaked one
        rooted value per extra argument, and given no arguments at all it
        handed back a second handle onto the *same* rooted value, so releasing
        either handle freed it under the other.

        Raises:
            TypeError: No arguments were given. Nix has no nullary
                application, so there is nothing for ``f()`` to mean.
        """
        if not arguments:
            raise TypeError("call() needs at least one argument; Nix has no nullary application")
        result = self.require_raw()
        owned = False  # `result` is still the caller's value until the first application
        try:
            for argument in arguments:
                applied = result.call(argument.require_raw())
                if owned:
                    result._release()  # type: ignore[reportPrivateUsage] -- L1 RootValue lifetime API  # noqa: SLF001
                result = applied
                owned = True
        except BaseException:
            if owned:
                result._release()  # type: ignore[reportPrivateUsage] -- L1 RootValue lifetime API  # noqa: SLF001
            raise
        return self._eval_state.wrap_value(result)

    def build(self, build_store: CoreStore | None, build_mode: int, eval_store: CoreStore | None) -> dict[str, object]:
        return self.require_raw().build(
            None if build_store is None else build_store.require_raw(),
            build_mode,
            None if eval_store is None else eval_store.require_raw(),
        )

    def derived_path(self) -> str:
        """Return this derivation's self-contained canonical DerivedPath string."""
        return self.require_raw().derived_path()


class CoreLockedFlake:
    """One in-memory locked flake, confined to its owning Nix thread."""

    def __init__(self, eval_state: CoreEvalState, raw: nanopynix_flake.LockedFlake) -> None:
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
    if isinstance(value, CoreValue):
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


class CoreRuntime:
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

    def open_store(self, uri: str) -> CoreStore:
        return CoreStore(self._core.open_store(uri))

    def open_eval_state(
        self,
        store: CoreStore,
        nix_path: Sequence[str],
        build_store: CoreStore | None = None,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> CoreEvalState:
        return CoreEvalState(
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
