"""Transport-neutral public protocols for nanopynix's asynchronous APIs.

The RPC and in-process implementations have different construction and
lifetime mechanics, but callers can depend on these shared operation shapes.
Protocols deliberately describe only behaviour common to both transports.
See :mod:`nanopynix.store` and :mod:`nanopynix.inproc` for the two
implementations, checked against these protocols in
``tests/nanopynix/test_protocols.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

from nanopynix_bindings.store import BuildMode

# LogLevel is a runtime import, unlike the type-only names below:
# AsyncEvalSession parameterises AsyncVerbosityController with it, and a base
# class expression is evaluated when the module loads.
from nanopynix_proto.nix.common import LogLevel
from nanopynix_proto.nix.store import GcAction, StoreDirs

from nanopynix._wire import DEFAULT_CA_METHOD, DEFAULT_HASH_ALGO, NO_GC_LIMIT

if TYPE_CHECKING:
    from nanopynix.models import (
        BuildResult,
        Derivation,
        FlakeRef,
        GcResult,
        GcRoot,
        MissingInfo,
        NixType,
        PathInfo,
        StorePath,
    )
    from nanopynix.settings import NixEvalSettings, NixFetchSettings
    from nanopynix.verbosity import LogLevelInput

VerbosityT_co = TypeVar("VerbosityT_co", covariant=True)


class AsyncValue(Protocol):
    """The common asynchronous value lifecycle and forcing interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def get_type(self) -> NixType:
        """Evaluate to weak head normal form and return the resulting Nix type.

        This is Nix's ``forceValue`` followed by ``value->type()``, which is
        how Nix itself uses forcing: the verb is never the goal, knowing what
        you have is. There is no separate ``force()`` because on both engines
        answering this question already forces.
        """
        ...

    async def to_python(self, *, copy_to_store: bool = False) -> Any:
        """Convert the whole value tree to plain Python data, using Nix's toJSON rules."""
        ...

    async def realise_string(self) -> str:
        """Coerce to a string and realise its Nix string context."""
        ...

    async def realise_argv(self) -> list[str]:
        """Coerce a Nix list to argv and realise all string contexts."""
        ...

    async def edit_location(self) -> tuple[str, int]:
        """Return the physical file path and line Nix would open for this value."""
        ...

    async def release(self) -> None:
        """Release the underlying worker-side handle. Idempotent."""
        ...

    # ── Typed extraction ───────────────────────────────────────────
    # Each forces to weak head normal form and raises if the value is not of
    # the requested type. `Self` rather than `AsyncValue` in the container
    # returns is what keeps an engine's values homogeneous: indexing an
    # inproc list yields an inproc Value, not "some AsyncValue".

    async def as_int(self) -> int:
        """Return this value as a Nix integer, or raise if it is not one."""
        ...

    async def as_float(self) -> float:
        """Return this value as a Nix float, or raise if it is not one."""
        ...

    async def as_bool(self) -> bool:
        """Return this value as a Nix boolean, or raise if it is not one."""
        ...

    async def as_string(self) -> str:
        """Return this value as a Nix string, or raise if it is not one."""
        ...

    async def as_list(self) -> list[Self]:
        """Return this value's elements, or raise if it is not a list."""
        ...

    async def as_dict(self) -> dict[str, Self]:
        """Return this value's attributes, or raise if it is not an attrset."""
        ...

    # ── Structure ──────────────────────────────────────────────────

    async def has_attr(self, name: str, /) -> bool:
        """Return whether this attrset has the attribute ``name``."""
        ...

    async def attr_names(self) -> list[str]:
        """Return this attrset's attribute names."""
        ...

    async def list_length(self) -> int:
        """Return the number of elements in this list."""
        ...

    # ── Application and realisation ────────────────────────────────
    #
    # Three members of the shared surface are deliberately absent, because no
    # signature would be true of both engines. All three are recorded in
    # tests/nanopynix/test_engine_parity.py, and that ledger -- not this
    # protocol -- is where the work to remove them is tracked:
    #
    #   attr, list_get  inproc awaits; rpc returns a proxy synchronously so a
    #                   chain costs one round trip instead of one per link.
    #                   (Value.attr:async, Value.list_get:async)
    #   call            inproc takes exactly one `argument`, rpc takes `*args`.
    #                   (Value.call:params)
    #
    # Declaring any of them would mean writing down a shape one engine does
    # not have, which is worse than the gap: it would make the conformance
    # test fail for a reason the reader cannot act on here.

    async def apply(self, function: str | Self, /) -> Self:
        """Apply ``function`` to this value, returning the unforced result."""
        ...

    async def auto_call(self) -> Self:
        """Auto-call a function using its default arguments, as ``nix-build`` does."""
        ...

    async def build(self, *, build_mode: BuildMode | int | None = None) -> dict[str, str]:
        """Realise this derivation, returning output name to store path.

        The engines' ``store`` parameter is omitted rather than declared: each
        accepts only its own ``Store``, and naming ``AsyncStore`` here would
        demand both accept the other's. Callers who need a separate build
        store reach for the concrete class.
        """
        ...


class AsyncStore(Protocol):
    """The common asynchronous Store lifecycle and path-query interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def open(self) -> None:
        """Open the underlying store."""
        ...

    async def close(self, *, force: bool = False) -> None:
        """Close the underlying store, optionally closing its evaluator first."""
        ...

    async def uri(self) -> str:
        """Return the canonical URI of this store."""
        ...

    async def store_dir(self) -> str:
        """Return this store's logical store directory."""
        ...

    async def parse_store_path(self, path: str, /) -> StorePath:
        """Validate and normalise ``path`` as a Nix store path."""
        ...

    async def is_valid_path(self, path: str | StorePath, /) -> bool:
        """Return whether ``path`` is valid in this store."""
        ...

    async def query_path_info(self, path: str | StorePath, /) -> PathInfo:
        """Return metadata for a valid store path."""
        ...

    async def query_all_valid_paths(self) -> list[StorePath]:
        """Return every valid path registered in this store."""
        ...

    async def compute_fs_closure(
        self,
        path: str | StorePath,
        /,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        """Return the filesystem closure of ``path``."""
        ...

    async def query_derivation_outputs(self, path: str | StorePath, /) -> list[StorePath]:
        """Return output paths declared by a derivation."""
        ...

    async def query_valid_derivers(self, path: str | StorePath, /) -> list[StorePath]:
        """Return valid derivations that produced ``path``."""
        ...

    async def query_referrers(self, path: str | StorePath, /) -> list[StorePath]:
        """Return valid store paths that reference ``path``."""
        ...

    async def follow_links_to_store_path(self, path: str, /) -> StorePath:
        """Resolve a path that may traverse symlinks to its containing store path."""
        ...

    async def query_path_from_hash_part(self, hash_part: str, /) -> StorePath | None:
        """Return the valid store path whose hash component is ``hash_part``, if any."""
        ...

    async def query_substitutable_paths(self, paths: list[str | StorePath], /) -> list[StorePath]:
        """Return the subset of ``paths`` that can be substituted from a binary cache."""
        ...

    async def get_build_log(self, path: str | StorePath, /) -> str | None:
        """Return the build log for ``path``, or ``None`` if no log is available."""
        ...

    async def query_missing(
        self,
        derived_paths: list[str | StorePath],
        /,
    ) -> MissingInfo:
        """Return which of ``derived_paths`` still need to be built or substituted."""
        ...

    async def build_paths_with_results(
        self,
        derived_paths: list[str | StorePath],
        /,
        *,
        build_mode: int = BuildMode.Normal.value,
        eval_store: Self | None = None,
    ) -> list[BuildResult]:
        """Build derived paths, treating a plain derivation path as all outputs.

        ``eval_store`` is ``Self``, not ``AsyncStore``: both engines reject an
        ``eval_store`` from a different session at runtime, so no
        implementation can honestly accept an arbitrary store here. Typing it
        as ``AsyncStore`` made every concrete class fail conformance, and the
        blanket suppression that hid it disabled the whole ``AsyncStore``
        check -- including, until this change, whether either engine had any
        GC-root methods at all.

        The cost, since ``Self`` is in an argument position: code written
        against ``AsyncStore`` rather than a concrete store can pass ``None``
        here but cannot thread a store through. That is a real restriction on
        engine-agnostic callers, and it is the honest one -- such a caller has
        no way to prove the store it holds came from the same session.
        """
        ...

    async def read_derivation(self, drv_path: str | StorePath, /) -> Derivation:
        """Parse and return the ``.drv`` file at ``drv_path``."""
        ...

    async def collect_garbage(
        self,
        action: GcAction,
        /,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: list[str | StorePath] | tuple[()] = (),
        max_freed: int = NO_GC_LIMIT,
    ) -> GcResult:
        """Run a garbage-collection pass; see :meth:`nanopynix.store.Store.collect_garbage`."""
        ...

    async def add_temp_root(self, path: str | StorePath, /) -> None:
        """Keep ``path`` alive against the collector for as long as this process holds the store."""
        ...

    async def add_perm_root(self, path: str | StorePath, gc_root: str, /) -> str:
        """Create the symlink ``gc_root`` -> ``path`` and return its resolved path."""
        ...

    async def add_indirect_root(self, path: str, /) -> None:
        """Register an existing user-facing symlink as an indirect root."""
        ...

    async def find_roots(self, *, censor: bool = False) -> list[GcRoot]:
        """Return the garbage collector's roots."""
        ...

    async def compute_store_path(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Compute the store path content-addressing ``path`` without adding it."""
        ...

    async def add_to_store(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Add a file or directory to this store, returning its store path."""
        ...

    async def store_dirs(self) -> StoreDirs:
        """Return this store's full set of configured directories."""
        ...

    async def ensure_path(self, path: str | StorePath, /) -> None:
        """Make ``path`` valid in this store, substituting it if necessary."""
        ...

    async def copy_closure(
        self,
        paths: list[str | StorePath],
        /,
        dest_store: Self,
        *,
        repair: bool = False,
        check_sigs: bool = True,
        substitute: bool = False,
    ) -> None:
        """Copy the closure of ``paths`` from this store to ``dest_store``.

        ``dest_store`` is ``Self`` for the same reason as
        :meth:`build_paths_with_results`'s ``eval_store`` -- both engines
        reject a store from another session, so no implementation can honestly
        accept an arbitrary ``AsyncStore`` here.
        """
        ...

    async def optimise_store(self) -> None:
        """Reclaim disk space by hard-linking identical files in this store."""
        ...

    async def verify_store(self, *, check_contents: bool = False, repair: bool = False) -> bool:
        """Check this store's consistency, returning whether errors were found."""
        ...


class AsyncLockedFlake(Protocol):
    """The common lifecycle for an in-memory flake lock."""

    async def eval(self) -> AsyncValue:
        """Evaluate this locked flake's outputs."""
        ...

    async def write_lock_file(self) -> None:
        """Persist this locked flake's lock file to disk."""
        ...

    async def release(self) -> None:
        """Release the worker-side handle for this locked flake. Idempotent."""
        ...


class AsyncVerbosityController(Protocol[VerbosityT_co]):
    """A resource that reads and updates the process-wide Nix verbosity.

    Defined here, above its first user, rather than at the end of the module:
    :class:`AsyncEvalSession` extends it.
    """

    async def get_verbosity(self) -> VerbosityT_co:
        """Return the current Nix log verbosity."""
        ...

    async def set_verbosity(self, verbosity: LogLevelInput) -> VerbosityT_co:
        """Set the Nix log verbosity and return the resulting level."""
        ...


class AsyncEvalSession[ValueT: AsyncValue = AsyncValue](AsyncVerbosityController[LogLevel], Protocol):
    """The common asynchronous evaluation and flake interface.

    Generic in the value type so an engine's evaluator yields that engine's
    values: ``inproc.EvalSession`` hands back ``inproc.Value``, not "some
    ``AsyncValue``". The parameter defaults to :class:`AsyncValue`, so a
    caller who does not care can still write a bare ``AsyncEvalSession``.

    Extends :class:`AsyncVerbosityController` rather than redeclaring
    ``get_verbosity``/``set_verbosity``: verbosity is process-wide, so an
    evaluator is one of several doors onto a single setting, not the owner of
    its own. A REPL is why the door is here at all -- ``pynix``'s
    ``:verbosity`` command holds a repl session and nothing else.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def open(self) -> None:
        """Create this session's evaluator. Called automatically by ``async with``."""
        ...

    async def close(self) -> None:
        """Release all values exported from this session and free the evaluator."""
        ...

    async def configure(
        self,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> None:
        """Apply live-mutable eval and fetch settings to the open evaluator."""
        ...

    async def file(self, path: str, /) -> ValueT:
        """Evaluate the Nix expression in the file at ``path``."""
        ...

    async def string(self, expr: str, path: str = "<string>", /) -> ValueT:
        """Evaluate the Nix expression ``expr``."""
        ...

    async def reset_file_cache(self) -> None:
        """Discard parsed file cache entries before re-evaluating source files."""
        ...

    async def lock_flake(
        self,
        ref: str,
        /,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
    ) -> AsyncLockedFlake:
        """Lock a flake, optionally updating inputs; see :meth:`nanopynix.rpc.EvalSession.lock_flake`."""
        ...

    async def eval_flake(self, ref: str, /, *, write_lock_file: bool = True) -> ValueT:
        """Lock and evaluate a flake in one step, returning its outputs."""
        ...

    async def get_flake(self, ref: str, /) -> FlakeRef:
        """Parse and resolve a flake reference without evaluating its outputs."""
        ...


class AsyncReplSession[ValueT: AsyncValue = AsyncValue](AsyncEvalSession[ValueT], Protocol):
    """An :class:`AsyncEvalSession` that keeps a persistent Nix lexical scope.

    Extending rather than standing alone mirrors both engines, whose
    ``ReplSession`` subclasses their ``EvalSession``. That is not incidental:
    a binding entered with :meth:`line` is only useful if :meth:`string` and
    :meth:`file` can then see it, so the repl surface and the eval surface
    are one interface.
    """

    @property
    def line_editors(self) -> tuple[str, ...]:
        """Editor-name substrings that support Nix's ``+LINE`` argument."""
        ...

    async def line(self, text: str, path: str = "<string>", /) -> ValueT | None:
        """Process one Nix REPL line. Bindings return ``None``; expressions return a value."""
        ...

    async def load_file(self, path: str, /) -> ValueT:
        """Load a Nix expression file as ``nix repl :load`` does."""
        ...

    async def add_attrs(self, value: ValueT, /) -> list[str]:
        """Add all attributes from ``value`` to this REPL's lexical scope."""
        ...

    async def scope_names(self) -> list[str]:
        """Return the identifiers visible in this REPL's lexical scope."""
        ...


__all__ = [
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
    "GcAction",
]
