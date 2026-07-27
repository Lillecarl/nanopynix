#include "py_store_impl.hh"

#include <cstdio>

// Required for the std::string / std::vector / std::optional / std::shared_ptr
// type casters. Without them nanobind has no caster for these types, so
// `nb::cast<std::string>` still compiles -- it falls back to the bound-type
// path -- and then fails at *runtime*. That is why a Python store had never
// successfully returned path info: every string field threw, and
// queryPathInfoUncached swallowed it. `shared_ptr.h` is what makes
// `PyStoreConfig::openStore`'s `nb::cast<std::shared_ptr<nix::Store>>` work;
// without it `underlying_store` raised `std::bad_cast` out of `open_store`, so
// every `if (underlying)` fallthrough in this file was unreachable.
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/shared_ptr.h>

#include <nix/store/path-info.hh>
#include <nix/store/realisation.hh>
#include <nix/util/hash.hh>
#include <nix/util/memory-source-accessor.hh>
#include <nix/util/callback.hh>

namespace nb = nanobind;

// The attribute `nanopynix.store_impl.StoreImpl.__init_subclass__` writes, and
// the only thing this file knows about the Python class hierarchy. Reading one
// well-known name keeps the dependency one-way: the bindings never import
// `nanopynix`.
static constexpr const char *OVERRIDES_ATTRIBUTE = "_nanopynix_store_overrides";

// Which operations the Python store actually implements.
//
// This used to be a per-call `hasattr` probe, which could not tell an
// implementation from a typo: misspell `query_path_info` and the store silently
// answered from the fallback instead of raising. `StoreImpl` records what a
// subclass really replaced, so the answer is exact -- and it is resolved once
// here rather than costing two attribute lookups under the GIL on every single
// store operation.
PyStoreMethods PyStoreMethods::resolve(const nb::object &py_store) {
    if (!nb::hasattr(py_store, OVERRIDES_ATTRIBUTE))
        throw nb::type_error(
            ("a Python store must subclass nanopynix.StoreImpl; open_store() returned an "
             "instance of '" + std::string(Py_TYPE(py_store.ptr())->tp_name) + "', which does not")
                .c_str());

    auto overrides = py_store.attr(OVERRIDES_ATTRIBUTE);
    auto implements = [&](const char *name) {
        return nb::cast<bool>(overrides.attr("__contains__")(nb::str(name)));
    };
    return PyStoreMethods{
        .is_valid_path_uncached = implements("is_valid_path_uncached"),
        .query_path_info = implements("query_path_info"),
        .query_path_from_hash_part = implements("query_path_from_hash_part"),
    };
}

// A dict entry that is present and not None.
//
// `contains()` alone is not enough, and getting that wrong was silently fatal:
// `path_info_to_dict` -- the outbound rendering a Python store would naturally
// mirror -- represents an absent optional as an explicit `None` rather than by
// omitting the key. `deriver`, `ca` and `registration_time` are all None for an
// ordinary locally-added path, so `nb::cast<std::string>(d["ca"])` threw on
// nearly every real store, and the exception was swallowed (see
// queryPathInfoUncached).
static std::optional<nb::object> dict_entry(const nb::dict &d, const char *key) {
    if (!d.contains(key)) return std::nullopt;
    nb::object value = nb::borrow<nb::object>(d[key]);
    if (value.is_none()) return std::nullopt;
    return value;
}

// One store path from a Python store, accepting either spelling.
//
// The Python side is handed base names (`StorePath::to_string()`), so a store
// that echoes back what it was given yields base names; a store that mirrors
// `path_info_to_dict` yields full `/nix/store/...` paths. Both are reasonable
// readings of the protocol and neither costs anything to accept.
static nix::StorePath store_path_from_py(const nix::Store &store, const nb::object &value) {
    auto text = nb::cast<std::string>(value);
    if (!text.empty() && text[0] == '/') return store.parseStorePath(text);
    return nix::StorePath{text};
}

// =========================================================================
// PyStoreImpl
// =========================================================================

PyStoreImpl::PyStoreImpl(nix::ref<const nix::StoreConfig> config, nb::object py_store,
                         std::shared_ptr<nix::Store> underlying)
    : Store(*config)
    , owned_config(config)
    , py_store(py_store)
    , methods(PyStoreMethods::resolve(py_store))
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
    if (methods.is_valid_path_uncached)
        return nb::cast<bool>(py_store.attr("is_valid_path_uncached")(
            nb::str(std::string(path.to_string()).c_str())));
    if (underlying) return underlying->isValidPath(path);
    return false;
}

void PyStoreImpl::queryPathInfoUncached(
    const nix::StorePath & path,
    nix::Callback<std::shared_ptr<const nix::ValidPathInfo>> callback) noexcept
{
    // Whether the Python store implements this at all is a different question
    // from whether its answer was usable, and the two must not share a handler.
    // Absence is a legitimate "not implemented" and falls through to the
    // underlying store; a raised exception is a real error and belongs to the
    // caller. This used to be one try block whose catch printed to stderr and
    // then fell through, so a store that raised -- or merely returned the same
    // shape `path_info_to_dict` produces -- handed back the underlying store's
    // answer as though it were its own.
    try {
        nb::gil_scoped_acquire gil;
        if (methods.query_path_info) {
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
            if (auto v = dict_entry(d, "nar_hash"))
                info->narHash = nix::Hash::parseAny(nb::cast<std::string>(*v), nix::HashAlgorithm::SHA256);
            if (auto v = dict_entry(d, "nar_size")) info->narSize = nb::cast<uint64_t>(*v);
            if (auto v = dict_entry(d, "references")) {
                for (auto ref : nb::cast<nb::list>(*v))
                    info->references.insert(store_path_from_py(*this, nb::borrow<nb::object>(ref)));
            }
            if (auto v = dict_entry(d, "registration_time")) info->registrationTime = nb::cast<time_t>(*v);
            if (auto v = dict_entry(d, "deriver")) info->deriver = store_path_from_py(*this, *v);
            if (auto v = dict_entry(d, "ca")) info->ca = nix::ContentAddress::parse(nb::cast<std::string>(*v));
            if (auto v = dict_entry(d, "ultimate")) info->ultimate = nb::cast<bool>(*v);
            // Same split as the outbound rendering in nix_store.cpp: 2.31 keeps
            // signatures as plain strings, 2.34+ as parsed nix::Signature.
            if (auto v = dict_entry(d, "sigs")) {
                for (auto sig : nb::cast<nb::list>(*v)) {
                    auto text = nb::cast<std::string>(nb::borrow<nb::object>(sig));
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
                    info->sigs.insert(text);
#else
                    info->sigs.insert(nix::Signature::parse(text));
#endif
                }
            }
            callback(info);
            return;
        }
    } catch (...) {
        callback.rethrow();
        return;
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
    if (methods.query_path_from_hash_part) {
        auto result = py_store.attr("query_path_from_hash_part")(nb::str(hashPart.c_str()));
        // Same two accepted spellings as the path fields of query_path_info.
        if (!result.is_none()) return store_path_from_py(*this, nb::borrow<nb::object>(result));
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

void PyStoreImpl::queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) {
    if (underlying) { underlying->queryReferrers(path, referrers); return; }
}

// The three below exist only to delegate to `underlying`. Each used to invent
// an answer when there was no underlying store, and each invented answer was
// wrong; deferring to `nix::Store` is both correct and less code. They cannot
// simply be deleted, because deleting them would drop the delegation too --
// `nix::Store` knows nothing about `underlying`.

nix::StorePathSet PyStoreImpl::queryAllValidPaths() {
    if (underlying) return underlying->queryAllValidPaths();
    // Was `{}`, i.e. "this store is empty" -- indistinguishable to a caller
    // from a store that really is empty. The base reports `unsupported`, which
    // is the honest answer for a store with no way to enumerate itself.
    return nix::Store::queryAllValidPaths();
}

nix::StorePathSet PyStoreImpl::querySubstitutablePaths(const nix::StorePathSet & paths) {
    if (underlying) return underlying->querySubstitutablePaths(paths);
    // Was `paths`, i.e. "everything you asked about has a substitute" -- so a
    // store reporting nothing valid still reported every path substitutable.
    // The base consults this store's substituters (2.34+) or returns {} (2.31).
    return nix::Store::querySubstitutablePaths(paths);
}

nix::StorePathSet PyStoreImpl::queryValidPaths(const nix::StorePathSet & paths, nix::SubstituteFlag f) {
    if (underlying) return underlying->queryValidPaths(paths, f);
    // Was `paths`, i.e. "every path you asked about is already valid". The base
    // filters by calling queryPathInfo per path, which dispatches back into the
    // Python store -- the answer it should have been giving all along.
    return nix::Store::queryValidPaths(paths, f);
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
