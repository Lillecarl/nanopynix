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

#include <nix/store/derived-path.hh>
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
    // `nb::inst_name`, and not `Py_TYPE(...)->tp_name`. The stable ABI build
    // of the wheel makes `PyTypeObject` an opaque struct, so that field does
    // not compile there. nanobind reads the name through `PyType_GetName`,
    // which the limited API has.
    if (!nb::hasattr(py_store, OVERRIDES_ATTRIBUTE))
        throw nb::type_error(
            ("a Python store must subclass nanopynix.StoreImpl; open_store() returned an "
             "instance of '" + nb::cast<std::string>(nb::inst_name(py_store)) + "', which does not")
                .c_str());

    auto overrides = py_store.attr(OVERRIDES_ATTRIBUTE);
    auto implements = [&](const char *name) {
        return nb::cast<bool>(overrides.attr("__contains__")(nb::str(name)));
    };
    PyStoreMethods methods;
#define NANOPYNIX_STORE_RESOLVE_FLAG(name) methods.name = implements(#name);
    NANOPYNIX_STORE_DISPATCH_METHODS(NANOPYNIX_STORE_RESOLVE_FLAG)
#undef NANOPYNIX_STORE_RESOLVE_FLAG

    // A warning stood here for the one operation a pre-2.32 build could not
    // dispatch: `Store::readDerivation` was non-virtual, so a store's override
    // was never called. Issue #126 raised the supported floor to 2.34, and
    // every supported version dispatches it, so there is nothing to warn about.
    return methods;
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

// A whole set of them, from any iterable a Python store cares to return -- a
// list, a set and a generator are all natural ways to answer these queries.
static nix::StorePathSet store_path_set_from_py(const nix::Store &store, const nb::object &value) {
    nix::StorePathSet paths;
    for (auto item : nb::cast<nb::iterable>(value))
        paths.insert(store_path_from_py(store, nb::borrow<nb::object>(item)));
    return paths;
}

// The outbound direction: base names, matching what every other call into the
// Python store hands it, so a store can pass its arguments straight through.
static nb::list store_paths_to_py(const nix::StorePathSet &paths) {
    nb::list out;
    for (const auto &path : paths)
        out.append(nb::str(std::string(path.to_string()).c_str()));
    return out;
}

// One store path as Python sees it.
static nb::str store_path_to_py(const nix::StorePath &path) {
    return nb::str(std::string(path.to_string()).c_str());
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
    // Every dispatch site in this file scopes the GIL to the Python call alone.
    // Holding it across `underlying->...` would block every other thread for the
    // length of a daemon round-trip, and would deadlock outright if the
    // underlying store ever called back into Python.
    {
        nb::gil_scoped_acquire gil;
        if (methods.is_valid_path_uncached)
            return nb::cast<bool>(py_store.attr("is_valid_path_uncached")(store_path_to_py(path)));
    }
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
                nix::UnkeyedValidPathInfo(
                    static_cast<const nix::StoreDirConfig &>(*this), nix::Hash::dummy));
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
                    info->sigs.insert(nix::Signature::parse(text));
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
    nix::Callback<std::shared_ptr<const nix::UnkeyedRealisation>> callback) noexcept
{
    if (underlying) { underlying->queryRealisation(id, std::move(callback)); return; }
    callback(nullptr);
}

std::optional<nix::StorePath> PyStoreImpl::queryPathFromHashPart(const std::string & hashPart) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_path_from_hash_part) {
            auto result = py_store.attr("query_path_from_hash_part")(nb::str(hashPart.c_str()));
            // A store that implements this has answered, including when it
            // answers None. This used to fall through to `underlying` on None,
            // so a store saying "I have no such path" was overruled by one that
            // did -- the same mistake as the queries that invented answers, and
            // inconsistent with is_valid_path_uncached and query_path_info,
            // which both treat the Python store's reply as final.
            if (result.is_none()) return std::nullopt;
            // Same two accepted spellings as the path fields of query_path_info.
            return store_path_from_py(*this, nb::borrow<nb::object>(result));
        }
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

std::shared_ptr<nix::SourceAccessor> PyStoreImpl::getFSAccessor(const nix::StorePath & path, bool requireValidPath) {
    if (underlying) return underlying->getFSAccessor(path, requireValidPath);
    return nullptr;
}

std::optional<nix::TrustedFlag> PyStoreImpl::isTrustedClient() {
    if (underlying) return underlying->isTrustedClient();
    return {nix::TrustedFlag::Trusted};
}

void PyStoreImpl::narFromPath(const nix::StorePath & path, nix::Sink & sink) {
    if (underlying) { underlying->narFromPath(path, sink); return; }
    unsupported("narFromPath");
}

// Every override below follows the same three-step order: dispatch to the
// Python store if it implements the operation, otherwise delegate to
// `underlying`, otherwise defer to `nix::Store`. The last step is what stops
// this class inventing answers -- for several of these the base is
// `unsupported()`, and "I cannot do this" is information a caller can act on in
// a way that a fabricated empty result is not.
//
// None of them may be deleted in favour of just inheriting: `nix::Store` knows
// nothing about `underlying`, so deleting an override drops the delegation too.

void PyStoreImpl::queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_referrers) {
            // "The result is not cleared" (store-api.hh), so insert rather than
            // assign -- computeFSClosure accumulates across calls.
            for (auto & referrer : store_path_set_from_py(
                     *this, py_store.attr("query_referrers")(store_path_to_py(path))))
                referrers.insert(referrer);
            return;
        }
    }
    if (underlying) { underlying->queryReferrers(path, referrers); return; }
    // Was a silent no-op, i.e. "nothing refers to this path" -- the same class
    // of invented answer as the queries below, and the reason a Python store
    // could never be garbage-collection-correct. The base reports unsupported.
    nix::Store::queryReferrers(path, referrers);
}

nix::StorePathSet PyStoreImpl::queryValidDerivers(const nix::StorePath & path) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_valid_derivers)
            return store_path_set_from_py(
                *this, py_store.attr("query_valid_derivers")(store_path_to_py(path)));
    }
    if (underlying) return underlying->queryValidDerivers(path);
    return nix::Store::queryValidDerivers(path);
}

nix::StorePathSet PyStoreImpl::queryDerivationOutputs(const nix::StorePath & path) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_derivation_outputs)
            return store_path_set_from_py(
                *this, py_store.attr("query_derivation_outputs")(store_path_to_py(path)));
    }
    if (underlying) return underlying->queryDerivationOutputs(path);
    // The base reads the derivation, which routes back through queryPathInfo
    // and therefore into the Python store.
    return nix::Store::queryDerivationOutputs(path);
}

nix::StorePathSet PyStoreImpl::queryAllValidPaths() {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_all_valid_paths)
            return store_path_set_from_py(*this, py_store.attr("query_all_valid_paths")());
    }
    if (underlying) return underlying->queryAllValidPaths();
    // Was `{}`, i.e. "this store is empty" -- indistinguishable to a caller
    // from a store that really is empty. The base reports `unsupported`, which
    // is the honest answer for a store with no way to enumerate itself.
    return nix::Store::queryAllValidPaths();
}

nix::StorePathSet PyStoreImpl::querySubstitutablePaths(const nix::StorePathSet & paths) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_substitutable_paths)
            return store_path_set_from_py(
                *this, py_store.attr("query_substitutable_paths")(store_paths_to_py(paths)));
    }
    if (underlying) return underlying->querySubstitutablePaths(paths);
    // Was `paths`, i.e. "everything you asked about has a substitute" -- so a
    // store reporting nothing valid still reported every path substitutable.
    // The base consults this store's substituters (2.34+) or returns {} (2.31).
    return nix::Store::querySubstitutablePaths(paths);
}

void PyStoreImpl::addTempRoot(const nix::StorePath & path) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.add_temp_root) {
            py_store.attr("add_temp_root")(store_path_to_py(path));
            return;
        }
    }
    if (underlying) { underlying->addTempRoot(path); return; }
    // The base debug-logs that the store does not support GC, which is exactly
    // true of a Python store that did not implement this.
    nix::Store::addTempRoot(path);
}

void PyStoreImpl::ensurePath(const nix::StorePath & path) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.ensure_path) {
            py_store.attr("ensure_path")(store_path_to_py(path));
            return;
        }
    }
    if (underlying) { underlying->ensurePath(path); return; }
    nix::Store::ensurePath(path);
}

void PyStoreImpl::optimiseStore() {
    {
        nb::gil_scoped_acquire gil;
        if (methods.optimise_store) {
            py_store.attr("optimise_store")();
            return;
        }
    }
    if (underlying) { underlying->optimiseStore(); return; }
    nix::Store::optimiseStore();  // a no-op, and honestly so
}

bool PyStoreImpl::verifyStore(bool checkContents, nix::RepairFlag repair) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.verify_store)
            // `repair` is lowered to a bool, matching how the Store binding
            // raises it back up (`repair ? nix::Repair : nix::NoRepair`), so
            // the Python signature is the same on both sides of the call.
            return nb::cast<bool>(py_store.attr("verify_store")(
                nb::bool_(checkContents), nb::bool_(repair == nix::Repair)));
    }
    if (underlying) return underlying->verifyStore(checkContents, repair);
    // The base returns false, i.e. "no errors remain". That is a claim, but it
    // is Nix's own claim for any store that does not implement verification.
    return nix::Store::verifyStore(checkContents, repair);
}

void PyStoreImpl::computeFSClosure(
    const nix::StorePathSet & paths,
    nix::StorePathSet & out,
    bool flipDirection,
    bool includeOutputs,
    bool includeDerivers) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.compute_fs_closure) {
            // Insert rather than assign, for a stronger reason than
            // queryReferrers': `computeClosure` uses this very set as its
            // visited set (`closure.hh:35`, `res.insert(current).second`), so
            // clearing it would lose the caller's accumulated result and, where
            // Nix accumulates across calls, could turn a completed traversal
            // back into a pending one.
            for (auto & p : store_path_set_from_py(
                     *this, py_store.attr("compute_fs_closure")(
                                store_paths_to_py(paths), nb::bool_(flipDirection),
                                nb::bool_(includeOutputs), nb::bool_(includeDerivers))))
                out.insert(p);
            return;
        }
    }
    if (underlying) {
        underlying->computeFSClosure(paths, out, flipDirection, includeOutputs, includeDerivers);
        return;
    }
    // The base walks the graph itself using queryPathInfo, queryReferrers and
    // queryValidDerivers -- all of which dispatch -- so a store that answers
    // those gets a correct closure without implementing this at all.
    nix::Store::computeFSClosure(paths, out, flipDirection, includeOutputs, includeDerivers);
}

nix::MissingPaths PyStoreImpl::queryMissing(const std::vector<nix::DerivedPath> & targets) {
    {
        nb::gil_scoped_acquire gil;
        if (methods.query_missing) {
            // Targets go over as the `^` strings Nix itself serializes derived
            // paths to, which is the only spelling that survives the trip: a
            // DerivedPath is a sum type, and `drv^out` and a bare `.drv` mean
            // different things ("build these outputs" vs "fetch this file").
            nb::list py_targets;
            for (const auto & target : targets)
                py_targets.append(nb::str(target.to_string(*this).c_str()));

            auto reply = nb::cast<nb::dict>(py_store.attr("query_missing")(py_targets));
            // Same shape `query_missing` renders outward (`nix_store.cpp`), so
            // a store may echo one back unchanged -- the property that keeps
            // query_path_info's round-trip test honest.
            nix::MissingPaths missing;
            if (auto v = dict_entry(reply, "will_build"))
                missing.willBuild = store_path_set_from_py(*this, *v);
            if (auto v = dict_entry(reply, "will_substitute"))
                missing.willSubstitute = store_path_set_from_py(*this, *v);
            if (auto v = dict_entry(reply, "unknown"))
                missing.unknown = store_path_set_from_py(*this, *v);
            if (auto v = dict_entry(reply, "download_size"))
                missing.downloadSize = nb::cast<uint64_t>(*v);
            if (auto v = dict_entry(reply, "nar_size"))
                missing.narSize = nb::cast<uint64_t>(*v);
            return missing;
        }
    }
    if (underlying) return underlying->queryMissing(targets);
    return nix::Store::queryMissing(targets);
}

nix::Derivation PyStoreImpl::readDerivation(const nix::StorePath & drvPath) {
    // The one dispatched operation whose Python return value is a
    // *serialization* rather than a rendering: the ATerm text of the `.drv`,
    // which is exactly the bytes a store holding one already has. Every other
    // method here answers with the same dict/list shape `nix_store.cpp` renders
    // outward, and mirroring that would mean rebuilding a `nix::Derivation`
    // from a dict -- five `DerivationOutput` variants, a `DerivedPathMap` tree
    // and `structuredAttrs`, each of which changed shape across the supported
    // versions. `parseDerivation` is Nix's own reader for this format, has an
    // identical signature on 2.31, 2.34 and 2.35, and is what
    // `readDerivationCommon` itself calls, so reusing it is both less code and
    // the only version of it guaranteed to agree with Nix.
    std::optional<std::string> aterm;
    {
        nb::gil_scoped_acquire gil;
        if (methods.read_derivation)
            aterm = nb::cast<std::string>(py_store.attr("read_derivation")(store_path_to_py(drvPath)));
    }
    // Parsed outside the GIL: it is pure C++ over a string we already own.
    //
    // The wrapping is `readDerivationCommon`'s, not decoration.
    // `parseDerivation` reports only what it choked on -- "expected string 'D'"
    // -- with no indication of *which* store object was unreadable, and this
    // caller has no file path to fall back on the way a real store does. Nix
    // adds the path for exactly that reason, and the empty case gets its own
    // message there too, which here is the likelier bug: a store returning ""
    // is a method that forgot to return, and "expected string 'D'" does not say
    // so. `msg()` rather than `message()` because 2.31 declares the latter
    // non-const (`error.hh:159`).
    if (aterm) {
        try {
            if (aterm->empty())
                throw nix::FormatError("derivation is empty (the store returned no ATerm text)");
            return nix::parseDerivation(config, std::move(*aterm), nix::Derivation::nameFromPath(drvPath));
        } catch (nix::FormatError & e) {
            throw nix::Error("error parsing derivation '%s': %s", printStorePath(drvPath), e.msg());
        }
    }
    if (underlying) return underlying->readDerivation(drvPath);
    return nix::Store::readDerivation(drvPath);
}

// --- not dispatched into Python; see the dispatch list in the header ---

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
