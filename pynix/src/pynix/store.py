from __future__ import annotations

from typing import override

from pynix import _impl
from pynix._cli import opt, pos
from pynix._settings import ConfiguredCommand, PynixCommand, store_option


class PrintRoots(ConfiguredCommand):
    """List the garbage collector roots"""

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_print_roots(self)


class PrintDead(ConfiguredCommand):
    """List paths that would be removed by a garbage collection.
    Use --rip to actually delete them."""

    rip: bool = opt(
        False,
        help="Actually delete the dead store paths instead of just listing them.",
    )

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_print_dead(self)


class PrintAlive(ConfiguredCommand):
    """List live paths in the store (reachable from GC roots)"""

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_print_alive(self)


class Gc(PynixCommand):
    """Manage Nix store garbage collection"""

    subcommands = (PrintRoots, PrintDead, PrintAlive)


class Info(ConfiguredCommand):
    """Show store metadata"""

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_info(self)


class Dirs(ConfiguredCommand):
    """Show configured local store directories"""

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_dirs(self)


class IsValidPath(ConfiguredCommand):
    """Check whether a store path is valid"""

    path: str = pos(help="Store path to check.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_is_valid_path(self)


class FollowLinksToStorePath(ConfiguredCommand):
    """Resolve symlinks to a store path"""

    path: str = pos(help="Filesystem path to resolve.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_follow_links_to_store_path(self)


class ComputeFsClosure(ConfiguredCommand):
    """Compute the filesystem closure of a store path"""

    path: str = pos(help="Store path to query.")

    flip_direction: bool = opt(False, help="Compute the inverse closure.")

    include_outputs: bool = opt(False, help="Include derivation outputs.")

    include_derivers: bool = opt(False, help="Include derivers.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_compute_fs_closure(self)


class QueryMissing(ConfiguredCommand):
    """Show which paths would need building, substituting, or are unknown"""

    paths: list[str] = pos(help="Store paths to query.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_query_missing(self)


class QueryDerivationOutputs(ConfiguredCommand):
    """Show the outputs of a derivation path"""

    path: str = pos(help="Derivation path to query.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_query_derivation_outputs(self)


class QueryValidDerivers(ConfiguredCommand):
    """Show valid derivers for a store path"""

    path: str = pos(help="Store path to query.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_query_valid_derivers(self)


class ListValidPaths(ConfiguredCommand):
    """List all valid paths in the store"""

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_list_valid_paths(self)


class QueryReferrers(ConfiguredCommand):
    """Show referrers of a store path"""

    path: str = pos(help="Store path to query.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_query_referrers(self)


class QuerySubstitutablePaths(ConfiguredCommand):
    """Show which paths are substitutable"""

    paths: list[str] = pos(help="Store paths to query.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_query_substitutable_paths(self)


class AddTempRoot(ConfiguredCommand):
    """Add a temporary GC root for this command's store session"""

    path: str = pos(help="Store path to root temporarily.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_add_temp_root(self)


class AddPermRoot(ConfiguredCommand):
    """Add a permanent GC root symlink"""

    path: str = pos(help="Store path to root.")

    gc_root: str = pos(help="GC root symlink to create.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_add_perm_root(self)


class AddIndirectRoot(ConfiguredCommand):
    """Register an indirect GC root"""

    path: str = pos(help="GC root path to register.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_add_indirect_root(self)


class PathFromHashPart(ConfiguredCommand):
    """Resolve a store path from its hash prefix"""

    hash_part: str = pos(help="Store path hash prefix to resolve.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_path_from_hash_part(self)


class EnsurePath(ConfiguredCommand):
    """Ensure a store path is valid, substituting it if available"""

    path: str = pos(help="Store path to ensure.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_ensure_path(self)


class Cat(ConfiguredCommand):
    """Print a file inside a local Nix store path"""

    path: str = pos(help="File path to print.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_cat(self)


class Ls(ConfiguredCommand):
    """List files inside a local Nix store path"""

    path: str = pos(help="File or directory path to list.")

    json: bool = opt(False, help="Print machine-readable JSON.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_ls(self)


class Add(ConfiguredCommand):
    """Add a file or directory to a Nix store"""

    path: str = pos(help="Filesystem path to add.")

    name: str | None = opt(None, short="n", help="Override the store path name component.")

    mode: str = opt("nar", help="Content-addressing method: nar, flat, or git.")

    hash_algo: str = opt("sha256", help="Hash algorithm to use.")

    dry_run: bool = opt(False, help="Compute the store path without adding the content.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_add(self)


class AddFile(ConfiguredCommand):
    """Add a single file to a Nix store"""

    path: str = pos(help="Filesystem path to add.")

    name: str | None = opt(None, short="n", help="Override the store path name component.")

    hash_algo: str = opt("sha256", help="Hash algorithm to use.")

    dry_run: bool = opt(False, help="Compute the store path without adding the content.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_add_file(self)


class AddPath(ConfiguredCommand):
    """Add a path to a Nix store using NAR ingestion"""

    path: str = pos(help="Filesystem path to add.")

    name: str | None = opt(None, short="n", help="Override the store path name component.")

    hash_algo: str = opt("sha256", help="Hash algorithm to use.")

    dry_run: bool = opt(False, help="Compute the store path without adding the content.")

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.store.run_add_path(self)


class DiffClosures(ConfiguredCommand):
    """Compare two filesystem closures"""

    before: str = pos(help="Original store path.")

    after: str = pos(help="New store path.")

    store: str = store_option("Store URI to query.")

    @override
    async def run(self) -> None:
        await _impl.store.run_diff_closures(self)


class Optimise(ConfiguredCommand):
    """Optimise store disk usage by hard-linking duplicate files"""

    store: str = store_option("Store URI to optimise.")

    @override
    async def run(self) -> None:
        await _impl.store.run_optimise(self)


class Verify(ConfiguredCommand):
    """Verify store integrity"""

    check_contents: bool = opt(False, help="Check path contents, not only metadata.")

    repair: bool = opt(False, help="Attempt repair while verifying.")

    store: str = store_option("Store URI to verify.")

    @override
    async def run(self) -> None:
        await _impl.store.run_verify(self)


class Store(PynixCommand):
    """Manage the Nix store"""

    subcommands = (
        Gc,
        Info,
        Dirs,
        IsValidPath,
        FollowLinksToStorePath,
        ComputeFsClosure,
        QueryMissing,
        QueryDerivationOutputs,
        QueryValidDerivers,
        ListValidPaths,
        QueryReferrers,
        QuerySubstitutablePaths,
        AddTempRoot,
        AddPermRoot,
        AddIndirectRoot,
        PathFromHashPart,
        EnsurePath,
        Cat,
        Ls,
        Add,
        AddFile,
        AddPath,
        DiffClosures,
        Optimise,
        Verify,
    )
