"""The Nix objects that ``inproc`` and the RPC worker share.

They hold huggorm's objects and are synchronous. ``inproc`` may share a
``CoreStore`` across the threads of its store pool, but each
``CoreEvalState`` and its values stay on one evaluator thread. The RPC worker
puts the same objects behind opaque remote handles.

``Core`` and not ``Local``: ``CoreStore`` holds any store it is given,
a ``unix://`` daemon store included, and ``nix::LocalStore`` is one kind.
"""

from __future__ import annotations

import inspect
import json
import time
import weakref
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from nanopynix_proto.nix.common import GcAction, StoreDirs

from nanopynix._core._extract import attrs_value_map, flake_ref_attrs
from nanopynix._core._nix_core import NixCore, nix_build_mode
from nanopynix._core._primops import registered_primops
from nanopynix._engine import (
    DerivedPathBuilt,
    EvalError,
    EvalState,
    KeyedBuildResult,
    LockedFlake,
    NixError,
    OutputsSpec,
    Value,
    errors as nanopynix_errors,
    fetchers as nanopynix_fetchers,
    parse_flake_ref,
    store as nanopynix_store,
)
from nanopynix._typechecking import BEARTYPING, no_runtime_type_check
from nanopynix._wire import DEFAULT_CA_METHOD, DEFAULT_HASH_ALGO, NO_GC_LIMIT, BuildMode
from nanopynix.exceptions import PrimopError
from nanopynix.models import (
    AttrDoc,
    BuildResult,
    Derivation,
    DerivationOutput,
    DerivationOutputs,
    Doc,
    FlakeRef,
    GcResult,
    GcRoot,
    JsonValue,
    LockedNode,
    MissingInfo,
    PathInfo,
    RealisedOutput,
    RegistryEntry,
    RegistryWrite,
    SettingsProvenance,
    StorePath,
)

_RAW_GC_ACTIONS = {
    GcAction.RETURN_LIVE: nanopynix_store.GCAction.ReturnLive,
    GcAction.RETURN_DEAD: nanopynix_store.GCAction.ReturnDead,
    GcAction.DELETE_DEAD: nanopynix_store.GCAction.DeleteDead,
    GcAction.DELETE_SPECIFIC: nanopynix_store.GCAction.DeleteSpecific,
}

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable, Mapping, Sequence

    from nanopynix._engine import BuildMode as NixBuildMode, Repl, StorePath as NixStorePath


@runtime_checkable
class _DerivedPathNode(Protocol):
    """The two fields that ``_derivation_outputs`` reads off one node.

    The engine's node is the real one, and it
    satisfies this by structure. A protocol, and not that class directly,
    because nanobind binds the class for reading only and it has no
    constructor. A test cannot build the tree that Nix will not produce on
    demand -- a child with several outputs, or a second level -- and
    ``test_the_input_drvs_builder_recurses_and_keeps_every_output`` covers
    exactly those two shapes from a stand-in node.
    """

    # A protocol with no `__slots__` gives every class that inherits it a
    # `__dict__`, and the cost is invisible.
    __slots__ = ()

    @property
    def outputs(self) -> list[str]: ...

    @property
    def dynamic_outputs(self) -> Mapping[str, _DerivedPathNode]: ...


def _registry_write(raw: Mapping[str, Any]) -> RegistryWrite:
    """One dict from the bindings, as the model the wire also carries.

    ``fetchers.pat`` declares the shape as ``RegistryWriteDict``, and that
    name is not the annotation here. It exists in the stub alone, and
    ``NANOPYNIX_BEARTYPING=1`` resolves every annotation at run time, so
    naming it would fail the import of this module.
    """
    return RegistryWrite(path=raw["path"], removed=raw["removed"], to=raw["to"], locked=raw["locked"])


def _build_result(result: KeyedBuildResult, store: Any) -> dict[str, object]:
    """One build's outcome: paths absolute, and statuses as words.

    *store* is huggorm's ``Store``: it spells the target, and its directory
    makes each path absolute.
    """
    target = result.path()
    rendered: str = store.print_derived_path(target)
    if isinstance(target, DerivedPathBuilt):
        spec = target.outputs()
        drv_path, outputs = rendered.rsplit("^", 1)[0], ["*"] if spec.all() else spec.names()
    else:
        drv_path, outputs = rendered, []
    prefix = f"{store.store_dir()}/"
    success, error = result.success(), result.error()
    built_outputs: dict[str, dict[str, object]] = {}
    if success is not None:
        status, error_msg = str(success.status()), ""
        built_outputs = {
            name: {
                "out_path": prefix + realisation.out_path().to_string(),
                "signatures": [signature.to_string() for signature in realisation.signatures()],
            }
            for name, realisation in success.built_outputs().items()
        }
    elif error is not None:
        # `colored` is Nix's message as it came; `message` strips it.
        status, error_msg = str(error.status), error.colored
    else:
        status, error_msg = "unknown", ""
    return {
        "drv_path": drv_path,
        "outputs": outputs,
        "success": success is not None,
        "status": status,
        "error_msg": error_msg,
        "built_outputs": built_outputs,
    }


def _derivation_outputs(node: _DerivedPathNode) -> DerivationOutputs:
    """Rebuild one node of Nix's ``DerivedPathMap`` tree, children included.

    ``dynamic_outputs`` nests once per level of dynamic derivation, so this
    recurses rather than handing the node straight to ``DerivationOutputs``.

    *node* carries fields, and it is not a dictionary. That is what makes
    each name below a checked one: pyright reads the field of the node and the
    field of the model, and it fails when the two stop agreeing. A ``**dict``
    spread type-checks against any shape at all, including a wrong one.
    """
    return DerivationOutputs(
        outputs=node.outputs,
        dynamic_outputs={name: _derivation_outputs(child) for name, child in node.dynamic_outputs.items()},
    )


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

    @staticmethod
    def _require_filesystem_path(path: str, method: str) -> None:
        """Reject the empty string for the methods that take a *filesystem* path.

        ``_store_path`` above forwards ``""`` to ``parse_store_path``, whose
        C++ guard rejects it. The three methods that take a filesystem path
        rather than a store path -- ``follow_links_to_store_path``,
        ``add_to_store`` and ``compute_store_path`` -- never reach that guard,
        and Nix resolves a relative path against the *process working
        directory*, so ``""`` silently becomes "whatever directory this process
        happens to be in".

        That is not a theoretical hazard. It is invisible from a source
        checkout, because the cwd is not in the store and Nix then errors
        anyway -- but run from a cwd that *is* inside the store (which is
        exactly what the packaged test runner does, since it ``cd``s into a
        store copy of the tree) and ``follow_links_to_store_path("")`` returns
        the cwd's store path as a perfectly good answer, on every supported Nix
        version. ``add_to_store("")`` would likewise have copied the working
        directory into the store.

        Nix's own behaviour here is neither safe nor consistent across
        versions: 2.35 and git raise a bare ``std::filesystem`` "cannot make
        absolute path: Invalid argument" ``RuntimeError`` instead, which is at
        least an error but not one a caller can distinguish from any other
        internal failure. Rejecting it here gives one answer everywhere, and
        the same ``BadStorePath`` every other path-taking entry point already
        produces for ``""``.
        """
        if path == "":
            raise nanopynix_errors.BadStorePath(f"{method}: the empty string is not a path")

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

        Derived paths, not store paths, and Nix's own reading of them: a
        plain ``.drv`` is an opaque fetch and selects **no** outputs, while
        Nix's ``^`` separator selects them. ``parse_derived_paths`` in C++
        hands both straight to ``nix::DerivedPath::parse``.

        The async ``Store`` of each engine is the layer that reads a bare
        ``.drv`` as every output, through
        :meth:`~nanopynix.models.DerivedPath.for_build`. This layer maps Nix
        and does not, so a caller here gets what ``nix build`` would give.
        """
        missing = self.require_raw().query_missing_typed([str(path) for path in derived_paths])
        return MissingInfo(
            will_build=missing.will_build,
            will_substitute=missing.will_substitute,
            unknown=missing.unknown,
            download_size=missing.download_size,
            nar_size=missing.nar_size,
        )

    def build_paths_with_results(
        self,
        derived_paths: Sequence[str | nanopynix_store.StorePath],
        *,
        build_mode: int,
        eval_store: CoreStore | None = None,
    ) -> list[BuildResult]:
        """Build ``derived_paths`` and return one result per Nix build outcome.

        Nix's reading of a derived path, as in :meth:`query_missing` above: a
        bare ``.drv`` here builds nothing.
        """
        results = self.require_raw().build_paths_with_results(
            [str(path) for path in derived_paths],
            build_mode,
            None if eval_store is None else eval_store.require_raw(),
        )
        return [
            BuildResult(
                drv_path=result["drv_path"],
                outputs=result["outputs"],
                success=result["success"],
                status=result["status"],
                error_msg=result["error_msg"],
                built_outputs={
                    name: RealisedOutput(out_path=output["out_path"], signatures=output["signatures"])
                    for name, output in result["built_outputs"].items()
                },
            )
            for result in results
        ]

    def build_targets(
        self,
        targets: list[NixStorePath | DerivedPathBuilt],
        build_mode: NixBuildMode,
        eval_store: CoreStore | None,
    ) -> list[dict[str, object]]:
        raw = self.require_raw().store
        results = raw.build_paths_with_results(
            targets, build_mode, None if eval_store is None else eval_store.require_raw().store
        )
        return [_build_result(result, raw) for result in results]

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
        self._require_filesystem_path(path, "follow_links_to_store_path")
        return StorePath(self.print_store_path(self.require_raw().follow_links_to_store_path(path)))

    def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        raw = self.require_raw().query_path_from_hash_part(hash_part)
        return None if raw is None else StorePath(self.print_store_path(raw))

    # --- Queries ----------------------------------------------------------

    def query_path_info(self, path: str | nanopynix_store.StorePath) -> PathInfo:
        info = self.require_raw().query_path_info_typed(self._store_path(path))
        return PathInfo(
            path=info.path,
            references=info.references,
            nar_hash=info.nar_hash,
            nar_size=info.nar_size,
            registration_time=info.registration_time,
            deriver=info.deriver,
            ca=info.ca,
            ultimate=info.ultimate,
            sigs=info.sigs,
        )

    def dump_db(
        self,
        paths: Sequence[str | nanopynix_store.StorePath],
        *,
        show_derivers: bool = True,
        show_hash: bool = True,
    ) -> str:
        return self.require_raw().dump_db(
            [self._store_path(path) for path in paths],
            show_derivers,
            show_hash,
        )

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
        drv = self.require_raw().read_derivation_typed(self._store_path(drv_path))
        # Every name below is a checked one. The bound type carries a real
        # annotation for each field, so pyright reads the binding and the model
        # together and fails when the two stop agreeing. The dictionary this
        # replaced could only be spread, and a `**dict` spread type-checks
        # against any shape at all -- including a wrong one.
        return Derivation(
            name=drv.name,
            system=drv.system,
            builder=drv.builder,
            args=drv.args,
            env=drv.env,
            input_srcs=drv.input_srcs,
            input_drvs={path: _derivation_outputs(node) for path, node in drv.input_drvs.items()},
            outputs={
                name: DerivationOutput(
                    type=output.type,
                    path=output.path,
                    ca=output.ca,
                    method=output.method,
                    hash_algo=output.hash_algo,
                )
                for name, output in drv.outputs.items()
            },
            structured_attrs=drv.structured_attrs,
        )

    # --- Mutation ---------------------------------------------------------

    def write_dev_shell_derivation(
        self,
        drv_path: str | nanopynix_store.StorePath,
        get_env_script: str,
    ) -> str:
        """Rewrite *drv_path* to dump its build environment, and store it.

        The rewrite itself is in C++, because the three supported Nix versions
        disagree on how a derivation gets written and how its output paths are
        filled. See ``write_dev_shell_derivation`` in ``nix_store.cpp``.
        """
        raw = self.require_raw().write_dev_shell_derivation(self._store_path(drv_path), get_env_script)
        return self.print_store_path(raw)

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
        self._require_filesystem_path(path, "add_to_store")
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
        self._require_filesystem_path(path, "compute_store_path")
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

    # --- The flake registry -----------------------------------------------

    def registry_entries(self, *, fetch_settings: Mapping[str, str] | None = None) -> list[RegistryEntry]:
        """Every registry entry Nix would consult, in the order Nix consults them.

        The store is here because the global layer downloads its file into
        one. Pass ``{"flake-registry": ""}`` to drop that layer, and no
        download or GC root happens. ``list_registry_entries`` in
        ``nix_fetchers.cpp`` gives the whole reason.
        """
        return [
            RegistryEntry(
                type=entry["type"],
                from_=entry["from"],
                to=entry["to"],
                exact=entry["exact"],
                extra_attrs=attrs_value_map(entry["extra_attrs"]),
            )
            for entry in nanopynix_fetchers.list_registry_entries(self.require_raw(), dict(fetch_settings or {}))
        ]

    def user_registry_path(self) -> str:
        """The registry file of the user, which is where a write goes by default."""
        return nanopynix_fetchers.user_registry_path()

    def registry_add(
        self,
        from_ref: str,
        to_ref: str,
        /,
        *,
        path: str | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> RegistryWrite:
        """Point ``from_ref`` at ``to_ref``, in one registry file.

        An empty ``path`` names the registry of the user. The write reads the
        file from disk each time, and not from Nix's per-process cache, so two
        writes to two files in one process do not read each other.
        ``registry_add`` in ``nix_fetchers.cpp`` gives the whole reason.
        """
        return _registry_write(
            nanopynix_fetchers.registry_add(path or "", from_ref, to_ref, dict(fetch_settings or {})),
        )

    def registry_remove(
        self,
        from_ref: str,
        /,
        *,
        path: str | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> RegistryWrite:
        """Drop every entry for ``from_ref``, from one registry file."""
        return _registry_write(
            nanopynix_fetchers.registry_remove(path or "", from_ref, dict(fetch_settings or {})),
        )

    def registry_pin(
        self,
        ref: str,
        locked: str | None = None,
        /,
        *,
        path: str | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> RegistryWrite:
        """Pin ``ref`` to the reference it resolves to now.

        ``locked`` names what to pin to, and an absent one pins ``ref`` to
        itself. **The call fetches**, because an unfetched reference has no
        revision to pin to. ``RegistryWrite.locked`` reports whether the
        result carries one.
        """
        return _registry_write(
            nanopynix_fetchers.registry_pin(
                self.require_raw(),
                path or "",
                ref,
                locked or "",
                dict(fetch_settings or {}),
            ),
        )

    @no_runtime_type_check  # action validates its own membership in _RAW_GC_ACTIONS at
    # runtime for untyped callers (see the KeyError guard below); beartype's
    # parameter check would otherwise intercept before that guard runs and
    # raise its own exception type instead of the documented ValueError.
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


def _sleep(seconds: object) -> bool:
    """``builtins.sleep``, for tests.

    It polls no interrupt while it waits, so a cancellation cannot stop it.
    That is what the cancel tests need.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        raise PrimopError(f"builtins.sleep takes a number of seconds, got {seconds!r}")
    if seconds < 0:
        raise PrimopError(f"builtins.sleep takes a number of seconds that is not negative, got {seconds:f}")
    time.sleep(seconds)
    return True


#: What ``to_json`` refuses, by huggorm's type name.
_NOT_JSON = frozenset({"function", "external"})


def _primop_argument(state: EvalState, primop: str, value: Value) -> object:
    """A primop's argument as JSON data, or an error naming what JSON cannot hold."""
    try:
        # Realised first: a primop may read a path that an argument names, so
        # the path has to exist.
        return json.loads(value.realise_json(False))
    except NixError as error:
        try:
            kind = _first_non_json(state, value)
        except NixError:
            kind = None
        if kind is None:
            raise
        raise EvalError(f"{primop}: argument contains non JSON-compatible Nix value of type '{kind}'") from error


def _first_non_json(state: EvalState, value: Value) -> str | None:
    state.force(value)
    kind = value.type_name()
    if kind in _NOT_JSON:
        return kind
    if kind == "list":
        items = (value.at(index) for index in range(value.length()))
    elif kind == "attrs":
        items = (value.get(name) for name in value.names())
    else:
        return None
    return next((found for item in items if (found := _first_non_json(state, item)) is not None), None)


def _make_scalar(state: EvalState, obj: object, context: list[str] | None) -> Value | None:
    match obj:
        case None:
            return state.make_null()
        case str():
            return state.make_string(obj, context)
        # Before `int`, because a bool is an int to `isinstance`.
        case bool():
            return state.make_bool(obj)
        case int():
            return state.make_int(obj)
        case float():
            return state.make_float(obj)
        case _:
            return None


class CoreEvalState:
    """One evaluator bound to a :class:`CoreStore`, confined to one Nix thread."""

    def __init__(self, raw: EvalState, store: CoreStore) -> None:
        self.raw: EvalState | None = raw
        self.store = store
        # Weak, because reference counting owns each value: a huggorm `Value`
        # roots its Nix value for as long as Python holds it. `close` frees the
        # values a caller still holds while the evaluator thread runs, so no
        # value outlives the evaluator on another thread.
        self._values: weakref.WeakSet[CoreValue] = weakref.WeakSet()
        self._locked_flakes: set[CoreLockedFlake] = set()
        self._repl: Repl | None = None
        # `__`, so it is `builtins.sleep` and does not shadow a `sleep` binding.
        raw.register_primop("__sleep", 1, self._primop("sleep", _sleep))
        for name, (arity, callback) in registered_primops().items():
            raw.register_primop(name, arity, self._primop(name, callback))

    def close(self) -> None:
        for value in tuple(self._values):
            value.close()
        for locked_flake in tuple(self._locked_flakes):
            locked_flake.close()
        self._repl = None
        self.raw = None

    def require_raw(self) -> EvalState:
        if self.raw is None:
            raise RuntimeError("local evaluator has been closed")
        return self.raw

    def wrap_value(self, raw: Value) -> CoreValue:
        value = CoreValue(self, raw)
        self._values.add(value)
        return value

    def discard_value(self, value: CoreValue) -> None:
        self._values.discard(value)

    def eval_string(self, expression: str, path: str = "<string>") -> CoreValue:
        return self.wrap_value(self.require_raw().eval_expr(expression, path))

    def eval_file(self, path: str) -> CoreValue:
        return self.wrap_value(self.require_raw().eval_file(path))

    def begin_repl(self) -> None:
        if self._repl is not None:
            raise RuntimeError("REPL scope is already active")
        self._repl = self.require_raw().repl()

    def repl_active(self) -> bool:
        self.require_raw()
        return self._repl is not None

    def _scope(self) -> Repl:
        self.require_raw()
        if self._repl is None:
            raise RuntimeError("REPL scope is not active")
        return self._repl

    def repl_eval_file(self, path: str) -> CoreValue:
        return self.wrap_value(self._scope().eval_file(path))

    def repl_eval_string(self, expression: str, path: str) -> CoreValue:
        return self.wrap_value(self._scope().eval_expr(expression, path))

    def repl_load_file(self, path: str) -> CoreValue:
        return self.wrap_value(self._scope().load_file(path))

    def repl_process_line(self, line: str, path: str) -> CoreValue | None:
        raw = self._scope().process_line(line, path)
        return None if raw is None else self.wrap_value(raw)

    def repl_add_attrs(self, value: CoreValue) -> list[str]:
        return self._scope().add_attrs(value.require_raw())

    def repl_scope_names(self) -> list[str]:
        return self._scope().names()

    def repl_select(self, expression: str, path: str = "<string>") -> tuple[str, CoreValue] | None:
        selected = self._scope().select(expression, path)
        if selected is None:
            return None
        return selected.name(), self.wrap_value(selected.attrs())

    def reset_file_cache(self) -> None:
        raw = self.require_raw()
        # `«nix-internal»/derivation-internal.nix` names no file to forget.
        for path in raw.cached_files():
            if path.startswith("/"):
                raw.forget_file(path)

    def statistics_json(self) -> str:
        self.require_raw()
        raise NotImplementedError("huggorm reports no evaluator statistics yet")

    def value_from_python(self, value: object) -> CoreValue:
        return self.wrap_value(self._make(value))

    def _make(self, obj: object, context: list[str] | None = None) -> Value:
        """*obj* as a Nix value; every string in it carries *context*."""
        state = self.require_raw()
        if isinstance(obj, CoreValue):
            return obj.require_raw()
        scalar = _make_scalar(state, obj, context)
        if scalar is not None:
            return scalar
        if isinstance(obj, list | tuple):
            made = state.make_list()
            for item in cast("list[object] | tuple[object, ...]", obj):
                state.list_append(made, self._make(item, context))
            return made
        if isinstance(obj, dict):
            made = state.make_attrs()
            for key, item in cast("dict[object, object]", obj).items():
                state.attrs_set(made, str(key), self._make(item, context))
            return made
        if callable(obj):
            return self._make_function(obj)
        raise TypeError(f"cannot make a Nix value from {type(obj).__name__}")

    def _make_function(self, callback: Callable[..., object]) -> Value:
        # The parameter count is the arity, and a callable with none, or with
        # no signature, is called now.
        try:
            arity = len(inspect.signature(callback).parameters)
        except (TypeError, ValueError):
            arity = 0
        if arity == 0:
            return self._make(callback())
        name = getattr(callback, "__qualname__", type(callback).__qualname__)
        return self.require_raw().make_primop(name, arity, self._primop(name, callback))

    def _primop(self, name: str, callback: Callable[..., object]) -> Callable[..., Value]:
        # Weak, because the state holds the bridge. A strong reference is a
        # cycle only the cyclic collector frees, and the store stays open until
        # it runs: 20 sessions with one primop held 16 descriptors of the
        # store's database. Nix calls the bridge only inside an evaluation,
        # which a caller reaches through this object, so it is alive then.
        owner = weakref.ref(self)

        def bridge(*arguments: Value) -> Value:
            evaluator = owner()
            if evaluator is None:
                raise RuntimeError("the evaluator that registered this primop is closed")
            state = evaluator.require_raw()
            converted = [_primop_argument(state, name, argument) for argument in arguments]
            # Every string the primop returns owes the store what its input
            # owed: a result that dropped the context would drop a dependency
            # from any closure built on it.
            context = sorted({element for argument in arguments for element in argument.string_context()})
            try:
                result = callback(*converted)
            except (PrimopError, ValueError) as error:
                # Both reject the input, so Nix shows their message bare.
                raise EvalError(str(error)) from error
            return evaluator._make(result, context)

        return bridge

    def lock_flake(
        self,
        ref: str,
        *,
        update_inputs: bool | list[str],
        write_lock_file: bool,
        flake_settings: Mapping[str, str] | None = None,
    ) -> CoreLockedFlake:
        """``update_inputs`` is True to recreate the lock file, or the inputs to update."""
        recreate, update = (update_inputs, []) if isinstance(update_inputs, bool) else (False, list(update_inputs))
        raw = self.require_raw().lock_flake(
            parse_flake_ref(ref),
            recreate=recreate,
            update=update,
            write_lock_file=write_lock_file,
            settings=dict(flake_settings or {}),
        )
        locked_flake = CoreLockedFlake(self, raw)
        self._locked_flakes.add(locked_flake)
        return locked_flake

    def call_locked_flake(self, locked_flake: CoreLockedFlake) -> CoreValue:
        return self.wrap_value(self.require_raw().call_flake(locked_flake.require_raw()))

    def get_flake(self, ref: str) -> FlakeRef:
        """Resolve a flake reference without evaluating its outputs."""
        return FlakeRef(attrs=flake_ref_attrs(self.require_raw().get_flake(parse_flake_ref(ref))))

    def eval_flake(
        self,
        ref: str,
        *,
        write_lock_file: bool,
        flake_settings: Mapping[str, str] | None = None,
    ) -> CoreValue:
        locked_flake = self.lock_flake(
            ref, update_inputs=False, write_lock_file=write_lock_file, flake_settings=flake_settings
        )
        try:
            return self.call_locked_flake(locked_flake)
        finally:
            locked_flake.close()

    def configure(
        self,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> None:
        """Apply live-mutable eval/fetch settings to this already-open evaluator.

        The backstop under both engines' ``configure()``. Those check the typed
        model, which is the check that gives a good message; this checks the
        rendered keys, which is all that reaches a worker over RPC. A worker
        must refuse what its client refuses, whatever built the request.

        The check runs before ``require_raw``, so a hand-built request meets
        the same answer whether or not the evaluator is still open.

        Raises:
            SettingNotLiveError: A key Nix reads only while constructing the
                evaluator.
        """
        eval_rendered = eval_settings or {}
        fetch_rendered = fetch_settings or {}
        from nanopynix.settings import (  # noqa: PLC0415 -- deferred import to avoid loading pydantic settings at startup
            NixEvalSettings,
            NixFetchSettings,
            reject_construction_time_keys,
        )

        reject_construction_time_keys(eval_rendered, model=NixEvalSettings, target="evaluator")
        reject_construction_time_keys(fetch_rendered, model=NixFetchSettings, target="evaluator")
        raw = self.require_raw()
        # huggorm's one setter tries the evaluator's settings, then the fetcher's.
        for name, value in (*eval_rendered.items(), *fetch_rendered.items()):
            raw.set_setting(name, value)

    def discard_locked_flake(self, locked_flake: CoreLockedFlake) -> None:
        self._locked_flakes.discard(locked_flake)


class CoreValue:
    """One rooted value, confined to its evaluator's Nix thread.

    huggorm refuses to read a thunk, so each read forces first, as Nix's own
    readers do. The predicates of the type do not force: a thunk answers
    ``"thunk"``.
    """

    def __init__(self, eval_state: CoreEvalState, raw: Value) -> None:
        self._eval_state = eval_state
        self._raw: Value | None = raw

    def close(self) -> None:
        self._raw = None
        self._eval_state.discard_value(self)

    def require_raw(self) -> Value:
        self._eval_state.require_raw()
        if self._raw is None:
            raise RuntimeError("local value has been released")
        return self._raw

    def _forced(self) -> Value:
        raw = self.require_raw()
        self._eval_state.require_raw().force(raw)
        return raw

    def _child(self, raw: Value) -> CoreValue:
        self._eval_state.require_raw().force(raw)
        return self._eval_state.wrap_value(raw)

    def force(self) -> None:
        self._forced()

    def to_json(self, copy_to_store: bool = False) -> JsonValue:
        return json.loads(self._forced().to_json(copy_to_store))

    def type_name(self) -> str:
        return self.require_raw().type_name()

    def as_int(self) -> int:
        return self._forced().integer()

    def as_float(self) -> float:
        # Nix's forceFloat widens an integer; huggorm's accessor reads one kind.
        raw = self._forced()
        if raw.type_name() == "int":
            return float(raw.integer())
        return raw.floating()

    def as_bool(self) -> bool:
        return self._forced().boolean()

    def as_string(self) -> str:
        return self._forced().string_value()

    def realise_string(self) -> str:
        return self._forced().realise_string()

    def realise_argv(self) -> list[str]:
        return self._forced().realise_argv()

    def edit_location(self) -> tuple[str, int]:
        location = self.require_raw().edit_location()
        return location.path(), location.line()

    def get_doc(self) -> Doc | None:
        doc = self.require_raw().doc()
        if doc is None:
            return None
        return Doc(name=doc.name(), args=doc.args(), arity=doc.arity(), doc=doc.doc(), path=doc.path(), line=doc.line())

    def attr_doc(self, name: str) -> AttrDoc | None:
        doc = self.require_raw().attr_doc(name)
        if doc is None:
            return None
        return AttrDoc(path=doc.path(), line=doc.line(), doc=doc.doc())

    def attr_get(self, name: str) -> CoreValue:
        return self._child(self._forced().get(name))

    def has_attr(self, name: str) -> bool:
        return self._forced().has(name)

    def attr_names(self) -> list[str]:
        return self._forced().names()

    def list_get(self, index: int) -> CoreValue:
        return self._child(self._forced().at(index))

    def list_length(self) -> int:
        return self._forced().length()

    def auto_call(self) -> CoreValue:
        """Apply with no arguments, as Nix's ``autoCallFunction`` does.

        That fills a lambda's defaulted formals, and follows ``__functor``. It
        answers anything else unapplied, where huggorm's ``apply_auto`` refuses.
        A new wrapper and not ``self``, because each caller closes what it holds.
        """
        raw = self._forced()
        if raw.type_name() == "attrs" and raw.has("__functor"):
            functor = self._child(raw.get("__functor"))
            try:
                applied = functor.call(self)
            finally:
                functor.close()
            try:
                return applied.auto_call()
            finally:
                applied.close()
        if raw.type_name() == "function" and raw.is_lambda() and raw.has_formals():
            return self._child(raw.apply_auto(self._eval_state.require_raw().make_attrs()))
        return self._eval_state.wrap_value(raw)

    def call(self, *arguments: CoreValue) -> CoreValue:
        """Apply this value as a Nix function to each argument in turn.

        Nix functions are curried -- ``f a b`` is ``(f a) b`` -- so more than
        one argument means more than one application. The partial results in
        between are values no caller sees, and rebinding ``result`` frees each.

        Raises:
            TypeError: No arguments were given. Nix has no nullary
                application, so there is nothing for ``f()`` to mean.
        """
        if not arguments:
            raise TypeError("call() needs at least one argument; Nix has no nullary application")
        state = self._eval_state.require_raw()
        result = self._forced()
        for argument in arguments:
            result = result.apply(argument.require_raw())
            state.force(result)
        return self._eval_state.wrap_value(result)

    def build(
        self,
        build_store: CoreStore | None = None,
        build_mode: int = BuildMode.Normal,
        eval_store: CoreStore | None = None,
    ) -> dict[str, object]:
        raw = self._forced()
        drv_path = raw.drv_path()
        output_paths = raw.output_paths()
        store = self._eval_state.store if build_store is None else build_store
        prefix = f"{self._eval_state.store.get_store_dir()}/"
        target = DerivedPathBuilt(drv_path, OutputsSpec(names=sorted(output_paths) or ["out"]))
        results = store.build_targets([target], nix_build_mode(build_mode), eval_store)
        return {
            "drv_path": prefix + drv_path.to_string(),
            "outputs": {name: prefix + path.to_string() for name, path in output_paths.items() if path is not None},
            "results": results,
        }

    def derived_path(self) -> str:
        """The ``.drv`` of this derivation, as an absolute path."""
        return f"{self._eval_state.store.get_store_dir()}/{self._forced().drv_path().to_string()}"


class CoreLockedFlake:
    """One in-memory locked flake, confined to its owning Nix thread."""

    def __init__(self, eval_state: CoreEvalState, raw: LockedFlake) -> None:
        self._eval_state = eval_state
        self._raw: LockedFlake | None = raw

    def close(self) -> None:
        self._raw = None
        self._eval_state.discard_locked_flake(self)

    def require_raw(self) -> LockedFlake:
        self._eval_state.require_raw()
        if self._raw is None:
            raise RuntimeError("local locked flake has been released")
        return self._raw

    def description(self) -> str:
        return self.require_raw().description() or ""

    def write_lock_file(self) -> None:
        self.require_raw().write_lock_file()

    def metadata_json(self) -> str:
        return self._eval_state.require_raw().flake_metadata_json(self.require_raw())

    def find_input(self, path: Sequence[str]) -> LockedNode | None:
        node = self.require_raw().find_input(list(path))
        if node is None:
            return None
        return LockedNode(
            locked_ref=node.locked_ref().to_string(),
            original_ref=node.original_ref().to_string(),
            is_flake=node.is_flake(),
        )


class CoreRuntime:
    """Common synchronous Nix runtime used by L2 and L3 on the Nix thread."""

    def __init__(self) -> None:
        self._core = NixCore()

    def initialize(
        self,
        *,
        settings: Mapping[str, str],
        experimental_features: Sequence[str],
        load_config: bool,
        verbosity: int | None,
    ) -> SettingsProvenance:
        return self._core.initialize(
            settings=settings,
            experimental_features=experimental_features,
            load_config=load_config,
            verbosity=verbosity,
        )

    # Not keyword-only: the worker dispatches this through `run_request`, which
    # forwards positional arguments only.
    def list_settings(self, overridden_only: bool = False) -> dict[str, str]:
        """Read Nix's global settings registry. See :meth:`NixCore.list_settings`."""
        return self._core.list_settings(overridden_only)

    def apply_settings(self, settings: Mapping[str, str]) -> dict[str, str]:
        """Write global settings, and read each back. See :meth:`NixCore.apply_settings`."""
        return self._core.apply_settings(settings)

    def open_store(self, uri: str) -> CoreStore:
        raw = self._core.open_store(uri)
        # `auto` alone cannot carry the connection limit that a daemon store
        # needs, so the limit goes on a second open. `resolve_auto_uri` gives
        # the reason. Both engines reach this method, so both get the rule.
        if uri == "auto" or uri.startswith("auto?"):
            from nanopynix.stores import (  # noqa: PLC0415 -- deferred import to avoid loading pydantic stores at startup
                resolve_auto_uri,
            )

            reopen_uri = resolve_auto_uri(uri, raw.get_uri())
            if reopen_uri is not None:
                # The first store never took a connection, so this releases
                # nothing. It runs because a store that this layer opened and does
                # not return is a store this layer closes.
                raw.close()
                raw = self._core.open_store(reopen_uri)
        return CoreStore(raw)

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

    def get_default_verbosity(self) -> int:
        return self._core.get_default_verbosity()
