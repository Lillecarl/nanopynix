#pragma once
///@file
/// The store operations that moved between Nix versions, under one name each.
///
/// **2.36 splits the build scheduler out of `nix::Store`.** `buildPaths`,
/// `buildPathsWithResults`, `buildDerivation`, `ensurePath` and `repairPath`
/// are methods of `nix::Builder` now, and `Store::getBuilder` returns one.
/// The store keeps a wrapper for some of them and not for others, so a caller
/// that wants one spelling on every supported version calls the functions
/// here.
///
/// `Store::getBuilder` with no argument returns a `LocalBuilder` over that
/// store, which is the object the removed `Store` methods reached. So each
/// function below does on 2.36 what the method did before it.

// **Before the Nix headers**, because the `#if` below reads a name it declares.
#include <nanopynix/nix_compat_config.hh>

#include <memory>
#include <vector>

#include <nix/store/build-result.hh>
#include <nix/store/derived-path.hh>
#include <nix/store/path.hh>
#include <nix/store/store-api.hh>
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
#  include <nix/store/build.hh>
#endif

namespace nanopynix::nix_compat {

/// Build each path, and report a result for each one rather than throwing.
inline std::vector<nix::KeyedBuildResult> build_paths_with_results(
    nix::Store & store,
    const std::vector<nix::DerivedPath> & paths,
    nix::BuildMode build_mode,
    std::shared_ptr<nix::Store> eval_store) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_36
    return store.buildPathsWithResults(paths, build_mode, std::move(eval_store));
#else
    return store.getBuilder(std::move(eval_store))->buildPathsWithResults(paths, build_mode);
#endif
}

/// Make a path valid, by substituting it when it is not valid already.
inline void ensure_path(nix::Store & store, const nix::StorePath & path) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_36
    store.ensurePath(path);
#else
    store.getBuilder()->ensurePath(path);
#endif
}

} // namespace nanopynix::nix_compat
