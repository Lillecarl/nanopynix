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
from nanopynix_proto.nix.store import GcAction

from nanopynix.models import NO_GC_LIMIT

if TYPE_CHECKING:
    from nanopynix.models import BuildResult, Derivation, GcResult, MissingInfo, PathInfo, StorePath
    from nanopynix.verbosity import LogLevelInput

ValueT = TypeVar("ValueT", bound="AsyncValue")
VerbosityT_co = TypeVar("VerbosityT_co", covariant=True)


class AsyncValue(Protocol):
    """The common asynchronous value lifecycle and forcing interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def force(self) -> Any:
        """Evaluate to weak head normal form."""
        ...

    async def force_deep(self) -> Any:
        """Recursively evaluate the entire value tree."""
        ...

    async def force_json(self, *, copy_to_store: bool = False) -> Any:
        """Serialize to JSON-compatible Python objects."""
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
        eval_store: AsyncStore | None = None,
    ) -> list[BuildResult]:
        """Build derived paths, treating a plain derivation path as all outputs."""
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


class AsyncEvalSession(Protocol):
    """The common asynchronous evaluation and flake interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def close(self) -> None:
        """Release all values exported from this session and free the evaluator."""
        ...

    async def file(self, path: str, /) -> AsyncValue:
        """Evaluate the Nix expression in the file at ``path``."""
        ...

    async def string(self, expr: str, path: str = "<string>", /) -> AsyncValue:
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
        """Lock a flake, optionally updating inputs; see :meth:`nanopynix.EvalSession.lock_flake`."""
        ...

    async def eval_flake(self, ref: str, /, *, write_lock_file: bool = True) -> AsyncValue:
        """Lock and evaluate a flake in one step, returning its outputs."""
        ...


class AsyncReplSession(Protocol[ValueT]):
    """The shared persistent REPL-scope operation interface."""

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

    async def reset_file_cache(self) -> None:
        """Discard parsed file cache entries before reloading REPL sources."""
        ...


class AsyncVerbosityController(Protocol[VerbosityT_co]):
    """A resource that reads and updates the process-wide Nix verbosity."""

    async def get_verbosity(self) -> VerbosityT_co:
        """Return the current Nix log verbosity."""
        ...

    async def set_verbosity(self, verbosity: LogLevelInput) -> VerbosityT_co:
        """Set the Nix log verbosity and return the resulting level."""
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
