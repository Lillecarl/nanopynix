#include "py_store_impl.hh"

#include <cstdio>

#include <nix/store/path-info.hh>
#include <nix/store/realisation.hh>
#include <nix/util/hash.hh>
#include <nix/util/memory-source-accessor.hh>
#include <nix/util/callback.hh>

namespace nb = nanobind;

static bool py_has_method(nb::object obj, const char *method) {
    return nb::hasattr(obj, method) && nb::isinstance<nb::callable>(obj.attr(method));
}

// =========================================================================
// PyStoreImpl
// =========================================================================

PyStoreImpl::PyStoreImpl(nix::ref<const nix::StoreConfig> config, nb::object py_store,
                         std::shared_ptr<nix::Store> underlying)
    : Store(*config)
    , py_store(std::move(py_store))
    , underlying(std::move(underlying))
{
    clearPathInfoCache();
}

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
#else
void PyStoreImpl::anchor() {}
#endif

bool PyStoreImpl::isValidPathUncached(const nix::StorePath & path) {
    nb::gil_scoped_acquire gil;
    if (py_has_method(py_store, "is_valid_path_uncached"))
        return nb::cast<bool>(py_store.attr("is_valid_path_uncached")(
            nb::str(std::string(path.to_string()).c_str())));
    if (underlying) return underlying->isValidPath(path);
    return false;
}

void PyStoreImpl::queryPathInfoUncached(
    const nix::StorePath & path,
    nix::Callback<std::shared_ptr<const nix::ValidPathInfo>> callback) noexcept
{
    try {
        nb::gil_scoped_acquire gil;
        if (py_has_method(py_store, "query_path_info")) {
            auto result = py_store.attr("query_path_info")(
                nb::str(std::string(path.to_string()).c_str()));
            if (result.is_none()) { callback(nullptr); return; }
            auto d = nb::cast<nb::dict>(result);
            auto info = std::make_shared<nix::ValidPathInfo>(
                nix::StorePath(path.to_string()),
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
                nix::UnkeyedValidPathInfo(nix::Hash::dummy)
#else
                nix::UnkeyedValidPathInfo(
                    static_cast<const nix::StoreDirConfig &>(*this), nix::Hash::dummy)
#endif
            );
            if (d.contains("nar_hash")) {
                info->narHash = nix::Hash::parseAny(nb::cast<std::string>(d["nar_hash"]), nix::HashAlgorithm::SHA256);
            }
            if (d.contains("nar_size")) info->narSize = nb::cast<uint64_t>(d["nar_size"]);
            if (d.contains("references")) {
                for (auto ref : nb::cast<nb::list>(d["references"]))
                    info->references.insert(nix::StorePath{nb::cast<std::string>(ref)});
            }
            if (d.contains("registration_time")) info->registrationTime = nb::cast<time_t>(d["registration_time"]);
            if (d.contains("ca")) info->ca = nix::ContentAddress::parse(nb::cast<std::string>(d["ca"]));
            callback(info);
            return;
        }
    } catch (std::exception &e) {
        fprintf(stderr, "nanopynix: Python query_path_info failed: %s; falling back to underlying store\n", e.what());
    }
    if (underlying) {
        auto info = underlying->queryPathInfo(path);
        // Convert to shared_ptr for callback
        callback(std::make_shared<const nix::ValidPathInfo>(*info));
        return;
    }
    callback(nullptr);
}

void PyStoreImpl::queryRealisationUncached(
    const nix::DrvOutput & id,
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
    nix::Callback<std::shared_ptr<const nix::Realisation>> callback
#else
    nix::Callback<std::shared_ptr<const nix::UnkeyedRealisation>> callback
#endif
    ) noexcept
{
    if (underlying) { underlying->queryRealisation(id, std::move(callback)); return; }
    callback(nullptr);
}

std::optional<nix::StorePath> PyStoreImpl::queryPathFromHashPart(const std::string & hashPart) {
    nb::gil_scoped_acquire gil;
    if (py_has_method(py_store, "query_path_from_hash_part")) {
        auto result = py_store.attr("query_path_from_hash_part")(nb::str(hashPart.c_str()));
        if (!result.is_none()) return nix::StorePath{nb::cast<std::string>(result)};
    }
    if (underlying) return underlying->queryPathFromHashPart(hashPart);
    return std::nullopt;
}

void PyStoreImpl::addToStore(const nix::ValidPathInfo & info, nix::Source & narSource,
                              nix::RepairFlag repair, nix::CheckSigsFlag checkSigs) {
    if (underlying) { underlying->addToStore(info, narSource, repair, checkSigs); return; }
    unsupported("addToStore");
}

nix::StorePath PyStoreImpl::addToStoreFromDump(
    nix::Source & dump, std::string_view name, nix::FileSerialisationMethod dm,
    nix::ContentAddressMethod hm, nix::HashAlgorithm ha,
    const nix::StorePathSet & refs, nix::RepairFlag repair) {
    if (underlying) return underlying->addToStoreFromDump(dump, name, dm, hm, ha, refs, repair);
    unsupported("addToStoreFromDump");
}

void PyStoreImpl::registerDrvOutput(const nix::Realisation & output) {
    if (underlying) { underlying->registerDrvOutput(output); return; }
}

nix::ref<nix::SourceAccessor> PyStoreImpl::getFSAccessor(bool requireValidPath) {
    if (underlying) return underlying->getFSAccessor(requireValidPath);
    return nix::make_ref<nix::MemorySourceAccessor>();
}

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
#else
std::shared_ptr<nix::SourceAccessor> PyStoreImpl::getFSAccessor(const nix::StorePath & path, bool requireValidPath) {
    if (underlying) return underlying->getFSAccessor(path, requireValidPath);
    return nullptr;
}
#endif

std::optional<nix::TrustedFlag> PyStoreImpl::isTrustedClient() {
    if (underlying) return underlying->isTrustedClient();
    return {nix::TrustedFlag::Trusted};
}

void PyStoreImpl::narFromPath(const nix::StorePath & path, nix::Sink & sink) {
    if (underlying) { underlying->narFromPath(path, sink); return; }
    unsupported("narFromPath");
}

nix::StorePathSet PyStoreImpl::queryAllValidPaths() {
    if (underlying) return underlying->queryAllValidPaths();
    return {};
}

void PyStoreImpl::queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) {
    if (underlying) { underlying->queryReferrers(path, referrers); return; }
}

nix::StorePathSet PyStoreImpl::querySubstitutablePaths(const nix::StorePathSet & paths) {
    if (underlying) return underlying->querySubstitutablePaths(paths);
    return paths;
}

nix::StorePathSet PyStoreImpl::queryValidPaths(const nix::StorePathSet & paths, nix::SubstituteFlag f) {
    if (underlying) return underlying->queryValidPaths(paths, f);
    return paths;
}

std::optional<std::string> PyStoreImpl::getVersion() {
    if (underlying) return underlying->getVersion();
    return {"python-store"};
}

void PyStoreImpl::addTempRoot(const nix::StorePath & path) {
    if (underlying) { underlying->addTempRoot(path); return; }
}

void PyStoreImpl::optimiseStore() {
    if (underlying) { underlying->optimiseStore(); return; }
}

// =========================================================================
// PyStoreConfig
// =========================================================================

PyStoreConfig::PyStoreConfig(
    std::string_view scheme, std::string_view authority, const Params & params,
    std::string name, std::string doc, nix::StringSet schemes, nb::object factory)
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
    : StoreConfig(params)
#else
    : StoreConfig(params, nix::StoreConfig::FilePathType::Unix)
#endif
    , py_factory(std::move(factory))
    , _name(std::move(name))
    , _doc(std::move(doc))
    , _schemes(std::move(schemes))
{
    pathInfoCacheSize = 0;
}

nix::ref<nix::Store> PyStoreConfig::openStore() const {
    nb::gil_scoped_acquire gil;
    auto py_store = py_factory.attr("open_store")();

    // Check if the Python store has an 'underlying' attribute
    std::shared_ptr<nix::Store> underlying;
    if (nb::hasattr(py_store, "underlying_store") && !py_store.attr("underlying_store").is_none()) {
        auto us = py_store.attr("underlying_store");
        if (nb::isinstance<nix::Store>(us)) {
            // It's a Store instance — get its shared_ptr
            // We can't easily get a shared_ptr from a reference, so we store
            // the Python reference and access the store through it
            underlying = nb::cast<std::shared_ptr<nix::Store>>(us);
        }
    }

    auto impl = std::make_shared<PyStoreImpl>(
        nix::ref<const nix::StoreConfig>(shared_from_this()), py_store, underlying);
    return nix::ref<nix::Store>(impl);
}

// =========================================================================
// Registration
// =========================================================================

void register_python_store(
    const std::string & name, const std::string & doc,
    const std::vector<std::string> & schemes, nb::object factory)
{
    nix::StringSet scheme_set(schemes.begin(), schemes.end());
    PyObject *raw_factory = factory.ptr();
    Py_INCREF(raw_factory);

    nix::StoreFactory store_factory{
        .doc = doc, .uriSchemes = scheme_set,
        .parseConfig = [name, doc, scheme_set, raw_factory](
            std::string_view scheme, std::string_view authority,
            const nix::Store::Config::Params & params) -> nix::ref<nix::StoreConfig>
        {
            nb::gil_scoped_acquire gil;
            auto f = nb::borrow<nb::object>(raw_factory);
            return nix::make_ref<PyStoreConfig>(scheme, authority, params, name, doc, scheme_set, std::move(f));
        },
        .getConfig = [name, doc, scheme_set, raw_factory]() -> nix::ref<nix::StoreConfig> {
            nb::gil_scoped_acquire gil;
            auto f = nb::borrow<nb::object>(raw_factory);
            nix::Store::Config::Params empty;
            return nix::make_ref<PyStoreConfig>(std::string_view{}, std::string_view{}, empty, name, doc, scheme_set, std::move(f));
        },
    };

    auto [it, didInsert] = nix::Implementations::registered().insert({name, std::move(store_factory)});
    if (!didInsert)
        throw std::runtime_error("store implementation '" + name + "' already registered");
}
