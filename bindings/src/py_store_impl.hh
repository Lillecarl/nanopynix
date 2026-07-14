#pragma once

#include <memory>
#include <string>

#include <nanobind/nanobind.h>

#include <nix/store/store-api.hh>
#include <nix/store/store-registration.hh>
#include <nix/store/path.hh>
#include <nix/util/ref.hh>
#include <nix/util/source-accessor.hh>

#include <nanopynix/nix_compat_config.hh>

namespace nb = nanobind;

/**
 * C++ trampoline that delegates Store virtual methods to a Python object.
 *
 * Every method checks whether the Python object has a corresponding method;
 * if so, calls it. Otherwise falls back to a sensible C++ default.
 */
struct PyStoreImpl : public nix::Store {
    nb::object py_store;
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
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
        nix::Callback<std::shared_ptr<const nix::Realisation>> callback
#else
        nix::Callback<std::shared_ptr<const nix::UnkeyedRealisation>> callback
#endif
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

    void registerDrvOutput(const nix::Realisation & output) override;

    nix::ref<nix::SourceAccessor> getFSAccessor(bool requireValidPath = true) override;
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
#else
    std::shared_ptr<nix::SourceAccessor> getFSAccessor(const nix::StorePath & path, bool requireValidPath = true) override;
#endif

    std::optional<nix::TrustedFlag> isTrustedClient() override;

    // --- optionally overridable ---
    void narFromPath(const nix::StorePath & path, nix::Sink & sink) override;
    nix::StorePathSet queryAllValidPaths() override;
    void queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) override;
    nix::StorePathSet querySubstitutablePaths(const nix::StorePathSet & paths) override;
    nix::StorePathSet queryValidPaths(const nix::StorePathSet & paths, nix::SubstituteFlag maybeSubstitute) override;
    std::optional<std::string> getVersion() override;
    void addTempRoot(const nix::StorePath & path) override;
    void optimiseStore() override;
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
