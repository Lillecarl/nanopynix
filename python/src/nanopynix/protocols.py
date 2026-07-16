"""Transport-neutral public protocols for nanopynix's asynchronous APIs.

The RPC and in-process implementations have different construction and
lifetime mechanics, but callers can depend on these shared operation shapes.
Protocols deliberately describe only behaviour common to both transports.
"""

from __future__ import annotations

from typing import Any, Protocol, Self, TypeVar

from nanopynix.models import PathInfo, StorePath
from nanopynix.verbosity import LogLevelInput

ValueT = TypeVar("ValueT", bound="AsyncValue")
VerbosityT_co = TypeVar("VerbosityT_co", covariant=True)


class AsyncValue(Protocol):
    """The common asynchronous value lifecycle and forcing interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def force(self) -> Any: ...

    async def force_deep(self) -> Any: ...

    async def force_json(self, *, copy_to_store: bool = False) -> Any: ...

    async def realise_string(self) -> str: ...

    async def realise_argv(self) -> list[str]: ...

    async def edit_location(self) -> tuple[str, int]: ...

    async def release(self) -> None: ...


class AsyncStore(Protocol):
    """The common asynchronous Store lifecycle and path-query interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def uri(self) -> str: ...

    async def store_dir(self) -> str: ...

    async def parse_store_path(self, path: str, /) -> StorePath: ...

    async def is_valid_path(self, path: str | StorePath, /) -> bool: ...

    async def query_path_info(self, path: str | StorePath, /) -> PathInfo: ...

    async def query_all_valid_paths(self) -> list[StorePath]: ...

    async def compute_fs_closure(
        self,
        path: str | StorePath,
        /,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]: ...

    async def query_derivation_outputs(self, path: str | StorePath, /) -> list[StorePath]: ...

    async def query_valid_derivers(self, path: str | StorePath, /) -> list[StorePath]: ...

    async def query_referrers(self, path: str | StorePath, /) -> list[StorePath]: ...

    async def follow_links_to_store_path(self, path: str, /) -> StorePath: ...

    async def query_path_from_hash_part(self, hash_part: str, /) -> StorePath | None: ...

    async def query_substitutable_paths(self, paths: list[str | StorePath], /) -> list[StorePath]: ...

    async def get_build_log(self, path: str | StorePath, /) -> str | None: ...


class AsyncLockedFlake(Protocol):
    """The common lifecycle for an in-memory flake lock."""

    async def eval(self) -> AsyncValue: ...

    async def write_lock_file(self) -> None: ...

    async def release(self) -> None: ...


class AsyncEvalSession(Protocol):
    """The common asynchronous evaluation and flake interface."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def close(self) -> None: ...

    async def file(self, path: str, /) -> AsyncValue: ...

    async def string(self, expr: str, path: str = "<string>", /) -> AsyncValue: ...

    async def lock_flake(
        self,
        ref: str,
        /,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
    ) -> AsyncLockedFlake: ...

    async def eval_flake(self, ref: str, /, *, write_lock_file: bool = True) -> AsyncValue: ...


class AsyncReplSession(Protocol[ValueT]):
    """The shared persistent REPL-scope operation interface."""

    async def line(self, text: str, path: str = "<string>", /) -> ValueT | None: ...

    async def load_file(self, path: str, /) -> ValueT: ...

    async def add_attrs(self, value: ValueT, /) -> list[str]: ...

    async def scope_names(self) -> list[str]: ...

    async def reset_file_cache(self) -> None: ...


class AsyncVerbosityController(Protocol[VerbosityT_co]):
    """A resource that reads and updates the process-wide Nix verbosity."""

    async def get_verbosity(self) -> VerbosityT_co: ...

    async def set_verbosity(self, verbosity: LogLevelInput) -> VerbosityT_co: ...


__all__ = [
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
]
