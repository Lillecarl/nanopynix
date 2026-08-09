#pragma once

#include <memory>
#include <string>

#include <nanobind/nanobind.h>

#include <nix/store/derivations.hh>
#include <nix/store/store-api.hh>
#include <nix/store/store-registration.hh>
#include <nix/store/path.hh>
#include <nix/util/ref.hh>
#include <nix/util/source-accessor.hh>

#include <nanopynix/nix_compat_config.hh>

namespace nb = nanobind;

/**
 * Every store operation that can be dispatched into Python, listed once.
 *
 * This is the single source of truth for three things that used to be three
 * hand-maintained lists: the flags on `PyStoreMethods`, the attribute reads in
 * `PyStoreMethods::resolve`, and the `STORE_DISPATCH_METHODS` tuple exported to
 * Python, from which `nanopynix.StoreImpl` derives its own list. Declaring a
 * method on `StoreImpl` that this file never calls is therefore no longer
 * possible -- previously it silently did nothing.
 *
 * The set mirrors the operations bound on the Python `Store` class: whatever a
 * caller can invoke on a store, a Python store can implement. What that class
 * binds but this list omits, and why:
 *
 * - `findRoots`, `getBuildLogExact`, `collectGarbage`, `addPermRoot` and
 *   `addIndirectRoot` are not `nix::Store` virtuals at all. They live on
 *   `GcStore`, `LogStore`, `LocalFSStore` and `IndirectRootStore`, and the
 *   bindings reach them through `require_*_store` casts that a `PyStoreImpl`
 *   fails regardless -- it does not inherit those interfaces. Adding hooks
 *   would need new base classes, not new dispatch.
 * - `computeStorePath` is a pure computation with no virtual to override.
 * - `addToStore`, `narFromPath`, `buildPaths` and `buildPathsWithResults` need
 *   a `Source &`, a `Sink &` or a `BuildResult`. Streaming and building cannot
 *   be expressed by a protocol whose whole shape is "return a dict, a string,
 *   or a list of strings", so they are excluded deliberately and permanently.
 * - `queryValidPaths` is not on that class, so nothing can call it directly;
 *   `nix::Store` derives it from `queryPathInfo`, which already dispatches.
 * `read_derivation` is dispatched because `nix::Store::readDerivation` is
 * virtual from 2.34 on (`store-api.hh:819`).
 * `nanopynix.build_info()["capabilities"]` carries
 * `store_impl_read_derivation`, the same way `dynamic_primop_registration` is
 * handled, for a caller that wants to branch on it.
 *
 * There is no fallback. `nix::Store::readDerivation` does *not* route through
 * `queryPathInfo`: `readDerivationCommon` reads the `.drv` through
 * `requireStoreObjectAccessor` (2.35, `store-api.cc:1183`), and `PyStoreImpl`
 * answers with a `nullptr` accessor when no `underlying` is set. Measured, not
 * inferred: on 2.35 a pure Python store whose `is_valid_path_uncached` returns
 * true and whose `query_path_info` answers still fails `read_derivation` with
 * `InvalidPath: path '...' is not a valid store path`. Serving derivations was
 * never possible without this hook, which is what makes it an addition rather
 * than a relaxation.
 */
#define NANOPYNIX_STORE_DISPATCH_METHODS(X)                                                        \
    X(is_valid_path_uncached)                                                                      \
    X(query_path_info)                                                                             \
    X(query_path_from_hash_part)                                                                   \
    X(query_referrers)                                                                             \
    X(query_valid_derivers)                                                                        \
    X(query_derivation_outputs)                                                                    \
    X(query_all_valid_paths)                                                                       \
    X(query_substitutable_paths)                                                                   \
    X(add_temp_root)                                                                               \
    X(ensure_path)                                                                                 \
    X(optimise_store)                                                                              \
    X(verify_store)                                                                                \
    X(compute_fs_closure)                                                                          \
    X(query_missing)                                                                               \
    X(read_derivation)

/**
 * Which of those a given Python store implements, resolved once at open time.
 */
struct PyStoreMethods {
#define NANOPYNIX_STORE_DISPATCH_FLAG(name) bool name = false;
    NANOPYNIX_STORE_DISPATCH_METHODS(NANOPYNIX_STORE_DISPATCH_FLAG)
#undef NANOPYNIX_STORE_DISPATCH_FLAG

    /** Throws `nb::type_error` if `py_store` is not a `nanopynix.StoreImpl`. */
    static PyStoreMethods resolve(const nb::object & py_store);
};

/**
 * C++ trampoline that delegates Store virtual methods to a Python object.
 *
 * An operation the Python store implements is dispatched to it. Anything else
 * falls through to `underlying` when one is set, and otherwise to
 * `nix::Store`'s own behaviour -- which is why this class overrides far fewer
 * virtuals than it once did. Overriding a query in order to answer it with a
 * guess is worse than not overriding it: `nix::Store` derives most answers from
 * `queryPathInfo`, which routes straight back into the Python store.
 */
struct PyStoreImpl : public nix::Store {
    /**
     * Keeps the config alive for as long as the store.
     *
     * `nix::Store` stores its config as a bare `const Config &`
     * (`store-api.hh:301`, bound in `Store::Store`), so it does not own it --
     * every concrete Nix store holds the owning ref itself, e.g. `LocalStore`
     * and `UDSRemoteStore`. Without this member the only owner was the
     * constructor's by-value argument, so the PyStoreConfig was freed as soon
     * as the constructor returned and `Store::config` dangled for the rest of
     * the store's life.
     */
    nix::ref<const nix::StoreConfig> owned_config;
    nb::object py_store;
    PyStoreMethods methods;
    std::shared_ptr<nix::Store> underlying;  // optional fallthrough store

    PyStoreImpl(nix::ref<const nix::StoreConfig> config, nb::object py_store,
                std::shared_ptr<nix::Store> underlying = {});

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
#else
    void anchor() override;
#endif

    // --- pure virtuals ---

    bool isValidPathUncached(const nix::StorePath & path) override;

    void queryPathInfoUncached(
        const nix::StorePath & path,
        nix::Callback<std::shared_ptr<const nix::ValidPathInfo>> callback) noexcept override;

    void queryRealisationUncached(
        const nix::DrvOutput & id,
        nix::Callback<std::shared_ptr<const nix::UnkeyedRealisation>> callback
        ) noexcept override;

    std::optional<nix::StorePath> queryPathFromHashPart(const std::string & hashPart) override;

    void addToStore(
        const nix::ValidPathInfo & info,
        nix::Source & narSource,
        nix::RepairFlag repair,
        nix::CheckSigsFlag checkSigs) override;

    nix::StorePath addToStoreFromDump(
        nix::Source & dump,
        std::string_view name,
        nix::FileSerialisationMethod dumpMethod,
        nix::ContentAddressMethod hashMethod,
        nix::HashAlgorithm hashAlgo,
        const nix::StorePathSet & references,
        nix::RepairFlag repair) override;

#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
    // Nix 2.36 makes `registerDrvOutputUnchecked` the pure virtual, and keeps
    // `registerDrvOutput(const Realisation &, CheckSigsFlag)` as a
    // signature-checking wrapper that calls it. This class holds the same
    // place in both shapes: the innermost operation.
    void registerDrvOutputUnchecked(const nix::Realisation & output) override;
#else
    void registerDrvOutput(const nix::Realisation & output) override;
#endif

    nix::ref<nix::SourceAccessor> getFSAccessor(bool requireValidPath = true) override;
    std::shared_ptr<nix::SourceAccessor> getFSAccessor(const nix::StorePath & path, bool requireValidPath = true) override;

    std::optional<nix::TrustedFlag> isTrustedClient() override;

    // --- optionally overridable ---

    // Dispatched to Python when implemented, then `underlying`, then nix::Store.
    void queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) override;
    nix::StorePathSet queryValidDerivers(const nix::StorePath & path) override;
    nix::StorePathSet queryDerivationOutputs(const nix::StorePath & path) override;
    nix::StorePathSet queryAllValidPaths() override;
    nix::StorePathSet querySubstitutablePaths(const nix::StorePathSet & paths) override;
    void addTempRoot(const nix::StorePath & path) override;
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
    // Nix 2.36 moves `ensurePath`, and the build entry points beside it, off
    // `nix::Store` and onto the `nix::Builder` that `getBuilder` returns. A
    // `PyStoreImpl` is not a `Builder`, so the Python `ensure_path` hook stays
    // reachable through a wrapper builder that `py_store_impl.cpp` defines.
    // `try_python_ensure_path` is the body that both bands share.
    nix::ref<nix::Builder> getBuilder(std::shared_ptr<nix::Store> evalStore = nullptr) override;
#else
    void ensurePath(const nix::StorePath & path) override;
#endif
    /// Give the Python `ensure_path` hook the first look at `path`.
    ///
    /// Returns true when the hook handled the call, and false when the caller
    /// must fall back.
    bool try_python_ensure_path(const nix::StorePath & path);
    void optimiseStore() override;
    bool verifyStore(bool checkContents, nix::RepairFlag repair) override;

    // `nix::Store` declares two `computeFSClosure` overloads and only the
    // set-taking one is virtual (`store-api.hh:836` on 2.34, `:979` on 2.35);
    // the single-path one just wraps it. Overriding the wrong
    // one would compile and never be called, so this is the set overload.
    void computeFSClosure(
        const nix::StorePathSet & paths,
        nix::StorePathSet & out,
        bool flipDirection,
        bool includeOutputs,
        bool includeDerivers) override;

    nix::MissingPaths queryMissing(const std::vector<nix::DerivedPath> & targets) override;

    nix::Derivation readDerivation(const nix::StorePath & drvPath) override;

    // Not dispatched: overridden only so `underlying` gets a chance before
    // nix::Store's own behaviour. `narFromPath` needs a `Sink &`, and
    // `queryValidPaths` is not part of the bound Store surface -- see the
    // dispatch-list comment above for why neither is a Python hook.
    void narFromPath(const nix::StorePath & path, nix::Sink & sink) override;
    nix::StorePathSet queryValidPaths(const nix::StorePathSet & paths, nix::SubstituteFlag maybeSubstitute) override;
    std::optional<std::string> getVersion() override;
};

/**
 * StoreConfig that creates PyStoreImpl instances from a Python factory.
 */
struct PyStoreConfig : public std::enable_shared_from_this<PyStoreConfig>, virtual nix::StoreConfig {
    nb::object py_factory;  // callable(self) -> Python store object
    std::string _name;       // store type name
    std::string _doc;        // documentation
    nix::StringSet _schemes; // URI schemes

    PyStoreConfig(
        std::string_view scheme,
        std::string_view authority,
        const Params & params,
        std::string name,
        std::string doc,
        nix::StringSet schemes,
        nb::object factory);

    static const std::string & name(const std::string & n) { static std::string s; s = n; return s; }
    static std::string doc(const std::string & d) { return d; }
    static nix::StringSet uriSchemes(const nix::StringSet & s) { return s; }

    nix::ref<nix::Store> openStore() const;

    nix::StoreReference getReference() const override {
        return {
            .variant = nix::StoreReference::Specified{
                .scheme = *_schemes.begin(),
            },
            .params = getQueryParams(),
        };
    }
};

/**
 * Register a Python-backed store implementation.
 *
 * After calling this, `nix::openStore("<scheme>://...")` will route to
 * the Python factory, which should return a Python store object.
 */
void register_python_store(
    const std::string & name,
    const std::string & doc,
    const std::vector<std::string> & schemes,
    nb::object factory);
