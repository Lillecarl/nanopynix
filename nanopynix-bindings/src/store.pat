# Each key below ends with `$`, and the anchor is load-bearing. nanobind's
# stubgen matches a key with `re.search` against the fully qualified name, so
# an unanchored key is a substring pattern: `Store.read_derivation` also
# matched `Store.read_derivation_typed` and gave the typed method the
# signature of the dictionary one. That defect is silent -- the stub still
# type-checks, and it describes the wrong return type.
# `tests/meta/test_stub_patterns.py` keeps every key anchored.
#
# **A name that ends in `Dict` is the shape of a dictionary that a helper
# returns. A bare name is a bound C++ type.** Issue #141 replaces each
# dictionary helper with a bound type, and the two live side by side until it
# finishes. Nix names the struct, so the bound type keeps that name
# (`Derivation`, `MissingPaths`) and the dictionary takes the suffix. Without
# the rule the two collide in one namespace, the later definition wins
# silently, and `read_derivation` reports the type of `read_derivation_typed`.
nanopynix_bindings.store.__prefix__$:
    class StoreDirsDict(TypedDict):
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

    class PathInfoDict(TypedDict):
        path: str
        references: list[str]
        nar_hash: str
        nar_size: int
        registration_time: int | None
        deriver: str | None
        ca: str | None
        ultimate: bool
        sigs: list[str]

    class MissingPathsDict(TypedDict):
        will_build: list[str]
        will_substitute: list[str]
        unknown: list[str]
        download_size: int
        nar_size: int

    class RealisedOutputDict(TypedDict):
        # One output that the build produced. `signatures` is empty for an
        # output this machine built; only a realisation from a substituter
        # under `ca-derivations` carries one.
        out_path: str
        signatures: list[str]

    class BuildResultDict(TypedDict):
        # `drv_path` is a store path; `outputs` says what was asked of it --
        # [] for an opaque fetch, ["*"] for every output, else named outputs.
        drv_path: str
        outputs: list[str]
        success: bool
        status: str
        error_msg: str
        # What the build produced, keyed by output name. Empty for a failure,
        # and for an opaque fetch. See build_result_util.hh, which also gives
        # the reason the timing fields of `BuildResult` are absent.
        built_outputs: dict[str, RealisedOutputDict]

    class DerivationOutputDict(TypedDict):
        # A tagged union rendered flat: `type` names the variant and decides
        # which of the remaining keys are present. InputAddressed sets `path`,
        # CAFixed sets `ca`, CAFloating and Impure set `method`/`hash_algo`,
        # and Deferred sets nothing else.
        type: str
        path: NotRequired[str]
        ca: NotRequired[str]
        method: NotRequired[str]
        hash_algo: NotRequired[str]

    class DerivationOutputsDict(TypedDict):
        # `dynamic_outputs` is a *tree*: Nix's DerivedPathMap nests one level
        # per level of dynamic derivation, so a child is another whole node and
        # not just an output name. Spelled recursively as a string annotation
        # because a TypedDict cannot refer to itself by name before it is bound.
        outputs: list[str]
        dynamic_outputs: dict[str, "DerivationOutputsDict"]

    class DerivationDict(TypedDict):
        name: str
        outputs: dict[str, DerivationOutputDict]
        input_srcs: list[str]
        input_drvs: dict[str, DerivationOutputsDict]
        system: str
        builder: str
        args: list[str]
        env: dict[str, str]
        # Nix's `__json` payload for a `__structuredAttrs = true` derivation.
        # None otherwise. Never present in `env` -- Nix moves it out.
        structured_attrs: str | None

    class GCResultsDict(TypedDict):
        paths: list[str]
        bytes_freed: int

    class GCRootDict(TypedDict):
        link: str
        path: str

    \from typing import NotRequired, TypedDict

nanopynix_bindings.store.StorePath.__eq__$:
    def __eq__(self, other: object) -> bool: ...

nanopynix_bindings.store.Store.get_store_dirs$:
    def get_store_dirs(self) -> StoreDirsDict: ...

nanopynix_bindings.store.Store.get_build_log$:
    def get_build_log(self, path: StorePath) -> str | None: ...

nanopynix_bindings.store.Store.query_path_info$:
    def query_path_info(self, path: StorePath) -> PathInfoDict: ...

# Both properties build an `nb::list`, which stubgen reports as a bare `list`.
# Each one holds strings. See bind_valid_path_info() in nix_store.cpp.
nanopynix_bindings.store.ValidPathInfo.references$:
    @property
    def references(self) -> list[str]: ...

nanopynix_bindings.store.ValidPathInfo.sigs$:
    @property
    def sigs(self) -> list[str]: ...

# The three sets of `MissingPaths`, for the same reason.
nanopynix_bindings.store.MissingPaths.will_build$:
    @property
    def will_build(self) -> list[str]: ...

nanopynix_bindings.store.MissingPaths.will_substitute$:
    @property
    def will_substitute(self) -> list[str]: ...

nanopynix_bindings.store.MissingPaths.unknown$:
    @property
    def unknown(self) -> list[str]: ...

# `Derivation` and `DerivationOutputs` build an `nb::list` or an `nb::dict`,
# which stubgen reports as a bare `list` or `dict`. Each one below names what
# it holds. See bind_derivation() and bind_derivation_outputs() in
# nix_store.cpp.
nanopynix_bindings.store.DerivationOutputs.outputs$:
    @property
    def outputs(self) -> list[str]: ...

# The tree of a `DerivedPathMap`: a child is another whole node, and not an
# output name. `DerivationOutputsDict` above says the same of the dictionary.
nanopynix_bindings.store.DerivationOutputs.dynamic_outputs$:
    @property
    def dynamic_outputs(self) -> dict[str, DerivationOutputs]: ...

nanopynix_bindings.store.Derivation.args$:
    @property
    def args(self) -> list[str]: ...

nanopynix_bindings.store.Derivation.env$:
    @property
    def env(self) -> dict[str, str]: ...

nanopynix_bindings.store.Derivation.input_srcs$:
    @property
    def input_srcs(self) -> list[str]: ...

nanopynix_bindings.store.Derivation.input_drvs$:
    @property
    def input_drvs(self) -> dict[str, DerivationOutputs]: ...

nanopynix_bindings.store.Derivation.outputs$:
    @property
    def outputs(self) -> dict[str, DerivationOutput]: ...

# Derived paths, not store paths: a plain .drv means all of that derivation's
# outputs and a ^ separator selects specific ones, which only the string form
# can carry. StorePath stays accepted so existing direct-API callers keep
# working -- see derived_path_strings() in nix_store.cpp.
nanopynix_bindings.store.Store.query_missing$:
    def query_missing(self, paths: Sequence[str | StorePath]) -> MissingPathsDict: ...

nanopynix_bindings.store.Store.query_missing_typed$:
    def query_missing_typed(self, paths: Sequence[str | StorePath]) -> MissingPaths: ...

nanopynix_bindings.store.Store.read_derivation$:
    def read_derivation(self, drv_path: StorePath) -> DerivationDict: ...

nanopynix_bindings.store.Store.collect_garbage$:
    def collect_garbage(self, action: GCAction, ignore_liveness: bool = False, paths_to_delete: Sequence[StorePath] = [], max_freed: int = 18446744073709551615) -> GCResultsDict: ...

nanopynix_bindings.store.Store.find_roots$:
    def find_roots(self, censor: bool = True) -> list[GCRootDict]: ...

# Derived paths, as query_missing above.
nanopynix_bindings.store.Store.build_paths_with_results$:
    def build_paths_with_results(self, paths: Sequence[str | StorePath], build_mode: BuildMode | int = BuildMode.Normal, eval_store: Store | None = None) -> list[BuildResultDict]: ...

# The queries below all funnel through store_paths_to_string_list(), which
# prints each StorePath -- so they hand back plain strings, not StorePath
# objects, despite the names.

nanopynix_bindings.store.Store.query_all_valid_paths$:
    def query_all_valid_paths(self) -> list[str]: ...

nanopynix_bindings.store.Store.compute_fs_closure$:
    def compute_fs_closure(self, path: StorePath, flip_direction: bool = False, include_outputs: bool = False, include_derivers: bool = False) -> list[str]: ...

nanopynix_bindings.store.Store.query_derivation_outputs$:
    def query_derivation_outputs(self, path: StorePath) -> list[str]: ...

nanopynix_bindings.store.Store.query_valid_derivers$:
    def query_valid_derivers(self, path: StorePath) -> list[str]: ...

nanopynix_bindings.store.Store.query_referrers$:
    def query_referrers(self, path: StorePath) -> list[str]: ...

nanopynix_bindings.store.Store.query_substitutable_paths$:
    def query_substitutable_paths(self, paths: Sequence[StorePath]) -> list[str]: ...

# stubgen writes module-level constants as their bare runtime type, so this
# arrives as `tuple` and every element is Unknown -- which propagates into
# `nanopynix.DISPATCHABLE_METHODS`, since that derives from this rather than
# restating the list.
nanopynix_bindings.store.STORE_DISPATCH_METHODS$:
    STORE_DISPATCH_METHODS: tuple[str, ...] = ...
