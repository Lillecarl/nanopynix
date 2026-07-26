nanopynix_bindings.store.__prefix__:
    class StoreDirs(TypedDict):
        # store_dir and uri are set unconditionally by store_dirs_to_dict();
        # the rest stay None unless the store is a LocalFSStore (root/state/log/
        # real_store) or a LocalStore (build). See src/nix_store.cpp.
        store_dir: str
        uri: str
        root_dir: str | None
        state_dir: str | None
        log_dir: str | None
        real_store_dir: str | None
        build_dir: str | None

    class PathInfo(TypedDict):
        path: str
        references: list[str]
        nar_hash: str
        nar_size: int
        registration_time: int | None
        deriver: str | None
        ca: str | None
        ultimate: bool

    class MissingPaths(TypedDict):
        will_build: list[str]
        will_substitute: list[str]
        unknown: list[str]
        download_size: int
        nar_size: int

    class BuildResult(TypedDict):
        drv_path: str
        success: bool
        status: str
        error_msg: str

    class DerivationOutput(TypedDict):
        # A tagged union rendered flat: `type` names the variant and decides
        # which of the remaining keys are present. InputAddressed sets `path`,
        # CAFixed sets `ca`, CAFloating and Impure set `method`/`hash_algo`,
        # and Deferred sets nothing else.
        type: str
        path: NotRequired[str]
        ca: NotRequired[str]
        method: NotRequired[str]
        hash_algo: NotRequired[str]

    class DerivationOutputs(TypedDict):
        outputs: list[str]
        dynamic_outputs: dict[str, str]

    class Derivation(TypedDict):
        name: str
        outputs: dict[str, DerivationOutput]
        input_srcs: list[str]
        input_drvs: dict[str, DerivationOutputs]
        system: str
        builder: str
        args: list[str]
        env: dict[str, str]

    class GCResults(TypedDict):
        paths: list[str]
        bytes_freed: int

    class GCRoot(TypedDict):
        link: str
        path: str

    \from typing import NotRequired, TypedDict

nanopynix_bindings.store.StorePath.__eq__:
    def __eq__(self, other: object) -> bool: ...

nanopynix_bindings.store.Store.get_store_dirs:
    def get_store_dirs(self) -> StoreDirs: ...

nanopynix_bindings.store.Store.get_build_log:
    def get_build_log(self, path: StorePath) -> str | None: ...

nanopynix_bindings.store.Store.query_path_info:
    def query_path_info(self, path: StorePath) -> PathInfo: ...

# Derived paths, not store paths: a plain .drv means all of that derivation's
# outputs and a ^ separator selects specific ones, which only the string form
# can carry. StorePath stays accepted so existing direct-API callers keep
# working -- see derived_path_strings() in nix_store.cpp.
nanopynix_bindings.store.Store.query_missing:
    def query_missing(self, paths: Sequence[str | StorePath]) -> MissingPaths: ...

nanopynix_bindings.store.Store.read_derivation:
    def read_derivation(self, drv_path: StorePath) -> Derivation: ...

nanopynix_bindings.store.Store.build_derivation:
    def build_derivation(self, drv_path: StorePath, build_mode: BuildMode | int = BuildMode.Normal) -> BuildResult: ...

nanopynix_bindings.store.Store.collect_garbage:
    def collect_garbage(self, action: GCAction, ignore_liveness: bool = False, paths_to_delete: Sequence[StorePath] = [], max_freed: int = 18446744073709551615) -> GCResults: ...

nanopynix_bindings.store.Store.find_roots:
    def find_roots(self, censor: bool = True) -> list[GCRoot]: ...

# Derived paths, as query_missing above.
nanopynix_bindings.store.Store.build_paths_with_results:
    def build_paths_with_results(self, paths: Sequence[str | StorePath], build_mode: BuildMode | int = BuildMode.Normal, eval_store: Store | None = None) -> list[BuildResult]: ...

# The queries below all funnel through store_paths_to_string_list(), which
# prints each StorePath -- so they hand back plain strings, not StorePath
# objects, despite the names.

nanopynix_bindings.store.Store.query_all_valid_paths:
    def query_all_valid_paths(self) -> list[str]: ...

nanopynix_bindings.store.Store.compute_fs_closure:
    def compute_fs_closure(self, path: StorePath, flip_direction: bool = False, include_outputs: bool = False, include_derivers: bool = False) -> list[str]: ...

nanopynix_bindings.store.Store.query_derivation_outputs:
    def query_derivation_outputs(self, path: StorePath) -> list[str]: ...

nanopynix_bindings.store.Store.query_valid_derivers:
    def query_valid_derivers(self, path: StorePath) -> list[str]: ...

nanopynix_bindings.store.Store.query_referrers:
    def query_referrers(self, path: StorePath) -> list[str]: ...

nanopynix_bindings.store.Store.query_substitutable_paths:
    def query_substitutable_paths(self, paths: Sequence[StorePath]) -> list[str]: ...
