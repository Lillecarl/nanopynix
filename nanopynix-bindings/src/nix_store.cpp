#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/typing.h>

#include <filesystem>
#include <limits>

#include <nix/store/store-api.hh>
#include <nix/store/store-open.hh>
#include <nix/store/gc-store.hh>
#include <nix/store/indirect-root-store.hh>
#include <nix/store/local-fs-store.hh>
#include <nix/store/local-store.hh>
#include <nix/store/log-store.hh>
#include <nix/store/names.hh>
#include <nix/store/path.hh>
#include <nix/store/derived-path.hh>
#include <nix/store/build-result.hh>
#include <nix/store/daemon.hh>
#include <nix/store/path-info.hh>
#include <nix/store/derivations.hh>
#include <nix/store/remote-store.hh>
#include <nix/store/content-address.hh>
#include <nix/store/store-reference.hh>
#include <nix/store/store-registration.hh>
#include <nlohmann/json.hpp>
#include <nix/util/experimental-features.hh>
// `experimentalFeatureSettings` itself, which write_dev_shell_derivation
// consults on Nix 2.31. The header above declares the feature enum only.
#include <nix/util/configuration.hh>
#include <nix/util/hash.hh>
#include <nix/util/serialise.hh>
#include <nix/util/file-descriptor.hh>
#include <nix/util/error.hh>
#include <nix/util/file-system.hh>
#include <nix/util/logging.hh>
#include <nix/util/source-accessor.hh>

#include <nanopynix/nix_compat_config.hh>

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
#include <nix/util/posix-source-accessor.hh>
#endif

#include "build_result_util.hh"
#include "nix_error_info.hh"
#include "py_store_impl.hh"

namespace nb = nanobind;
using namespace nb::literals;

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
using NixGCAction = nix::GCOptions::GCAction;
constexpr auto gcReturnLive = nix::GCOptions::gcReturnLive;
constexpr auto gcReturnDead = nix::GCOptions::gcReturnDead;
constexpr auto gcDeleteDead = nix::GCOptions::gcDeleteDead;
constexpr auto gcDeleteSpecific = nix::GCOptions::gcDeleteSpecific;
#else
using NixGCAction = nix::GCAction;
constexpr auto gcReturnLive = nix::GCAction::gcReturnLive;
constexpr auto gcReturnDead = nix::GCAction::gcReturnDead;
constexpr auto gcDeleteDead = nix::GCAction::gcDeleteDead;
constexpr auto gcDeleteSpecific = nix::GCAction::gcDeleteSpecific;
#endif

template<typename Path>
static std::string nix_path_to_string(const Path &path) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
    return path;
#else
    return path.string();
#endif
}

// =========================================================================
// Helpers — convert Nix types to nb::dict for Pydantic validation
// =========================================================================

static std::string store_path_to_string(nix::Store &s, const nix::StorePath &sp) {
    return s.printStorePath(sp);
}

static nb::list store_paths_to_string_list(nix::Store &s, const nix::StorePathSet &paths) {
    nb::list result;
    for (auto &p : paths) result.append(store_path_to_string(s, p));
    return result;
}

static nb::list string_set_to_list(const nix::StringSet &values) {
    nb::list result;
    for (auto &value : values) result.append(value);
    return result;
}

static nb::dict path_info_to_dict(nix::Store &s, const nix::ValidPathInfo &info) {
    nb::dict d;
    d["path"] = store_path_to_string(s, info.path);

    // references — list of store path dicts
    nb::list refs;
    for (auto &r : info.references) refs.append(store_path_to_string(s, r));
    d["references"] = refs;

    d["nar_hash"] = info.narHash.to_string(nix::HashFormat::SRI, true);
    d["nar_size"] = nb::int_(info.narSize);

    if (info.registrationTime)
        d["registration_time"] = nb::int_(info.registrationTime);
    else
        d["registration_time"] = nb::none();

    if (info.deriver)
        d["deriver"] = store_path_to_string(s, *info.deriver);
    else
        d["deriver"] = nb::none();

    if (info.ca)
        d["ca"] = nix::renderContentAddress(*info.ca);
    else
        d["ca"] = nb::none();

    d["ultimate"] = info.ultimate;

    // Narinfo signatures. Nix changed the field's element type -- 2.31 stores
    // plain strings, 2.34+ stores parsed `nix::Signature` with `toStrings` to
    // render them back. The guard is spelled 2.32 because that is the constant
    // this file already has and it is correct for every version actually built
    // (2.31, 2.34, 2.35); the exact release that changed it was not measured.
    nb::list sigs;
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
    for (auto &sig : info.sigs) sigs.append(sig);
#else
    for (auto &sig : nix::Signature::toStrings(info.sigs)) sigs.append(sig);
#endif
    d["sigs"] = sigs;

    return d;
}

// =========================================================================
// StorePath
// =========================================================================

static void bind_store_path(nb::module_ &m) {
    nb::class_<nix::StorePath>(m, "StorePath")
        .def(nb::init<const std::string &>(), "path"_a,
             "Parse a store path from its basename (e.g. '<hash>-<name>')")
        .def("to_string", [](const nix::StorePath &sp) { return std::string(sp.to_string()); })
        .def("name", [](const nix::StorePath &sp) { return std::string(sp.name()); })
        .def("hash_part", [](const nix::StorePath &sp) { return std::string(sp.hashPart()); })
        .def("is_derivation", &nix::StorePath::isDerivation)
        .def("__str__", [](const nix::StorePath &sp) { return std::string(sp.to_string()); })
        .def("__repr__", [](const nix::StorePath &sp) {
            return "StorePath('" + std::string(sp.to_string()) + "')";
        })
        .def("__eq__", [](const nix::StorePath &a, const nix::StorePath &b) { return a == b; })
        .def("__hash__", [](const nix::StorePath &sp) {
            return std::hash<std::string_view>{}(sp.to_string());
        });
}

// =========================================================================
// Store — bound directly via shared_ptr<Store>
// =========================================================================

static std::shared_ptr<nix::Store> open_store_uri(const std::string &uri) {
    nb::gil_scoped_release release;
    return nix::openStore(uri).get_ptr();
}
static std::shared_ptr<nix::Store> open_store_default() {
    nb::gil_scoped_release release;
    return nix::openStore().get_ptr();
}

static void close_store(nix::Store &store) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
    // Nix 2.31 has no RemoteStore::shutdownConnections(). The Python owner
    // drops its final shared_ptr immediately after this call, so the
    // RemoteStore destructor closes the pooled daemon connections via RAII.
    (void) store;
#else
    if (auto *remote_store = dynamic_cast<nix::RemoteStore *>(&store)) {
        remote_store->shutdownConnections();
    }
#endif
}

static void process_connection(std::shared_ptr<nix::Store> store, int fd, bool trusted, bool recursive) {
    auto from = nix::FdSource(nix::toDescriptor(fd));
    auto to = nix::FdSink(nix::toDescriptor(fd));
    nix::daemon::processConnection(
        nix::ref<nix::Store>(store),
        std::move(from), std::move(to),
        trusted ? nix::TrustedFlag::Trusted : nix::TrustedFlag::NotTrusted,
        recursive ? nix::daemon::RecursiveFlag::Recursive : nix::daemon::RecursiveFlag::NotRecursive);
}
// --- PathInfo ---

static nb::dict query_path_info(nix::Store &s, const nix::StorePath &path) {
    std::optional<nix::ref<const nix::ValidPathInfo>> info;
    {
        nb::gil_scoped_release release;
        info.emplace(s.queryPathInfo(path));
    }
    return path_info_to_dict(s, **info);
}

// The text behind `nix-store --dump-db`, which `nix-store --load-db` reads
// back. Nix calls makeValidityRegistration once for each argument
// (`nix-store.cc`, opDumpDB), so the records come out in the order the caller
// gave. One call with the whole set would sort them by store path instead,
// because the parameter is a StorePathSet. The order does not change the
// database that `--load-db` builds: LocalStore::registerValidPaths adds every
// path first, and resolves the references in a second pass
// (`local-store.cc`). Argument order is kept because it makes this function
// byte-identical to the command.
//
// makeValidityRegistration is not virtual and is not in
// NANOPYNIX_STORE_DISPATCH_METHODS. It needs no entry there: nix::Store
// derives the whole text from queryPathInfo, which a Python store already
// answers.
//
// The caller owns the closure. Nix says so at the definition, and it is true
// here: this function registers exactly the paths it gets. A record whose
// references are absent gives a database that names a path it does not have.
static std::string dump_db(nix::Store &s, const std::vector<nix::StorePath> &paths,
                           bool show_derivers, bool show_hash) {
    std::string out;
    {
        nb::gil_scoped_release release;
        for (const auto &path : paths)
            out += s.makeValidityRegistration({path}, show_derivers, show_hash);
    }
    return out;
}

// --- Closures ---

static nb::list compute_fs_closure(nix::Store &s, const nix::StorePath &path,
                                    bool flip, bool include_outputs, bool include_derivers) {
    nix::StorePathSet out;
    {
        nb::gil_scoped_release release;
        s.computeFSClosure(path, out, flip, include_outputs, include_derivers);
    }
    return store_paths_to_string_list(s, out);
}

// --- MissingInfo ---

// Accept either spelling Python has for a store path: the plain string a
// caller typed, or a StorePath the bindings previously handed back. Only the
// string form can carry a ^ output selector, but making callers stringify
// first would break every existing direct-API caller to gain nothing.
static std::vector<std::string> derived_path_strings(const nb::sequence &paths, const char *op) {
    std::vector<std::string> raw;
    for (nb::handle item : paths) {
        if (nb::isinstance<nix::StorePath>(item)) {
            raw.push_back(std::string(nb::cast<nix::StorePath>(item).to_string()));
            continue;
        }
        if (!nb::isinstance<nb::str>(item))
            throw nix::UsageError("%s: derived paths must be str or StorePath", op);
        raw.push_back(nb::cast<std::string>(item));
    }
    return raw;
}

// The one place a caller-supplied derived path becomes a nix::DerivedPath.
// Shared by the direct bindings and (until it goes) the proto-dict funnel, so
// the two cannot drift -- they did, and the ^ selector below is what they
// drifted over: the dict path honoured it and the direct path rejected it as
// an illegal character in a store path name.
static nix::DerivedPaths parse_derived_paths(
        nix::Store &s, std::vector<std::string> raw, const char *op) {
    nix::DerivedPaths paths;
    paths.reserve(raw.size());
    for (auto &path : raw) {
        if (path.empty())
            throw nix::BadStorePath("%s: store path must not be empty", op);
        if (path[0] != '/') path = s.config.storeDir_ + "/" + path;
        // Nix's own parser, and nothing on top of it. A string with `^` is a
        // `Built`, and one without is an `Opaque` -- including a bare `.drv`,
        // which asks the store for the derivation *file* and builds none of
        // its outputs.
        //
        // **A bare `.drv` used to become Built{All} right here, and that is
        // now done in Python.** The convenience is real and both engines still
        // give it (`models.DerivedPath.for_build`, applied by each async
        // `Store` before it reaches this function). It does not belong in a
        // binding that maps a Nix function: `nix build <drv>` is `Opaque` too
        // (`installable-derived-path.cc:32-37`), so doing otherwise made this
        // the one place where nanopynix and the `nix` CLI disagreed on the
        // meaning of one string, and left a direct caller unable to ask for
        // the opaque fetch at all.
        paths.push_back(nix::DerivedPath::parse(s.config, path));
    }
    return paths;
}

static nb::dict query_missing(nix::Store &s, const nix::DerivedPaths &paths) {
    nix::MissingPaths m;
    {
        nb::gil_scoped_release release;
        m = s.queryMissing(paths);
    }
    nb::dict d;
    d["will_build"] = store_paths_to_string_list(s, m.willBuild);
    d["will_substitute"] = store_paths_to_string_list(s, m.willSubstitute);
    d["unknown"] = store_paths_to_string_list(s, m.unknown);
    d["download_size"] = nb::int_(m.downloadSize);
    d["nar_size"] = nb::int_(m.narSize);
    return d;
}

// The direct binding. Takes derived paths, like nix::Store::queryMissing
// itself does -- the StorePath-only variant this replaces could not express
// an output selector, which is exactly how inproc and rpc came to disagree.
static nb::dict query_missing_paths(nix::Store &s, const nb::sequence &paths) {
    return query_missing(s, parse_derived_paths(s, derived_path_strings(paths, "query_missing"), "query_missing"));
}

// --- Collective queries ---

static nb::list query_derivation_outputs(nix::Store &s, const nix::StorePath &path) {
    nix::StorePathSet paths;
    {
        nb::gil_scoped_release release;
        paths = s.queryDerivationOutputs(path);
    }
    return store_paths_to_string_list(s, paths);
}
static nb::list query_valid_derivers(nix::Store &s, const nix::StorePath &path) {
    nix::StorePathSet paths;
    {
        nb::gil_scoped_release release;
        paths = s.queryValidDerivers(path);
    }
    return store_paths_to_string_list(s, paths);
}
static nb::list query_all_valid_paths(nix::Store &s) {
    nix::StorePathSet paths;
    {
        nb::gil_scoped_release release;
        paths = s.queryAllValidPaths();
    }
    return store_paths_to_string_list(s, paths);
}
static nb::list query_referrers(nix::Store &s, const nix::StorePath &path) {
    nix::StorePathSet refs;
    {
        nb::gil_scoped_release release;
        s.queryReferrers(path, refs);
    }
    return store_paths_to_string_list(s, refs);
}
static nb::list query_substitutable_paths(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::StorePathSet ps(paths.begin(), paths.end());
    nix::StorePathSet subs;
    {
        nb::gil_scoped_release release;
        subs = s.querySubstitutablePaths(ps);
    }
    return store_paths_to_string_list(s, subs);
}

// --- GC / roots / maintenance ---

// The four require_*_store helpers below throw nix::Unsupported, not
// nix::Error. Both describe the same condition, but only Unsupported is
// registered as a Python type (nix_store.cpp's NB_MODULE), so only it arrives
// as nanopynix.UnsupportedError carrying Nix's ErrorInfo; a bare nix::Error
// lands outside the NixError hierarchy as a plain RuntimeError with the
// position and trace discarded. It is also what Nix itself uses for "this
// store cannot do that".

static nix::GcStore &require_gc_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::GcStore *>(&s);
    if (store == nullptr)
        throw nix::Unsupported("store '%s' does not support garbage collection", s.config.getHumanReadableURI());
    return *store;
}

static nix::LocalFSStore &require_local_fs_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::LocalFSStore *>(&s);
    if (store == nullptr)
        throw nix::Unsupported("store '%s' does not support local filesystem roots", s.config.getHumanReadableURI());
    return *store;
}

static nix::IndirectRootStore &require_indirect_root_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::IndirectRootStore *>(&s);
    if (store == nullptr)
        throw nix::Unsupported("store '%s' does not support indirect roots", s.config.getHumanReadableURI());
    return *store;
}

static nix::LogStore &require_log_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::LogStore *>(&s);
    if (store == nullptr)
        throw nix::Unsupported("store '%s' does not support retrieving build logs", s.config.getHumanReadableURI());
    return *store;
}

// The four ingredients addToStoreSlow and computeStorePath both need. These
// take plain arguments rather than an nb::dict so the native and proto-shaped
// entrypoints share one definition instead of two copies of the same prelude.
// An empty method/hash_algo means "unset" on the proto side, where a missing
// string field arrives as "" rather than absent -- both fall back to Nix's own
// defaults rather than being passed to a parser that would reject them.
static nix::ContentAddressMethod parse_content_address_method(const std::string &raw) {
    return nix::ContentAddressMethod::parse(raw.empty() ? std::string("nar") : raw);
}

static nix::HashAlgorithm parse_store_hash_algo(const std::string &raw) {
    return nix::parseHashAlgo(raw.empty() ? std::string("sha256") : raw);
}

// Nix names a content-addressed path after its source's filename unless the
// caller overrides it.
static std::string store_add_name(const std::optional<std::string> &name, const std::string &path) {
    if (name)
        return *name;
    return std::filesystem::path(path).filename().string();
}

static nix::SourcePath source_path_from(const std::string &path) {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
    return nix::PosixSourceAccessor::createAtRoot(
        nix::makeParentCanonical(std::filesystem::path(path)));
#else
    return nix::SourcePath(nix::makeFSSourceAccessor(
        std::filesystem::absolute(std::filesystem::path(path))));
#endif
}

static NixGCAction gc_action_from_int(int action) {
    switch (action) {
        case 1: return gcReturnLive;
        case 2: return gcReturnDead;
        case 3: return gcDeleteDead;
        case 4: return gcDeleteSpecific;
        default: return gcReturnDead;
    }
}

static nb::list find_roots(nix::Store &s, bool censor) {
    // Nix 2.34.7 creates non-PID temp-root filenames but still parses every
    // temp-root filename with std::stoi. See https://github.com/NixOS/nix/issues/16138.
    nix::Roots roots;
    {
        nb::gil_scoped_release release;
        roots = require_gc_store(s).findRoots(censor);
    }
    nb::list result;
    for (auto &[target, links] : roots) {
        for (auto &link : links) {
            nb::dict root;
            root["link"] = link;
            root["path"] = store_path_to_string(s, target);
            result.append(root);
        }
    }
    return result;
}

static nb::dict collect_garbage(
        nix::Store &s,
        NixGCAction action,
        bool ignore_liveness,
        const std::vector<nix::StorePath> &paths_to_delete,
        uint64_t max_freed) {
    nix::GCOptions options;
    options.action = action;
    options.ignoreLiveness = ignore_liveness;
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
    options.pathsToDelete = nix::StorePathSet(paths_to_delete.begin(), paths_to_delete.end());
#else
    if (paths_to_delete.empty()) {
        options.pathsToDelete = nix::GCOptions::WholeStore{};
    } else {
        options.pathsToDelete = nix::GCOptions::SpecificPaths{
            .paths = nix::StorePathSet(paths_to_delete.begin(), paths_to_delete.end()),
        };
    }
#endif
    options.maxFreed = max_freed;

    nix::GCResults results;
    {
        nb::gil_scoped_release release;
        require_gc_store(s).collectGarbage(options, results);
    }

    nb::dict d;
    d["paths"] = string_set_to_list(results.paths);
    d["bytes_freed"] = nb::int_(results.bytesFreed);
    return d;
}

// --- Build ---

static nb::list build_derived_paths_with_results(
        nix::Store &s,
        const nix::DerivedPaths &paths,
        nix::BuildMode buildMode = nix::bmNormal,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    std::vector<nix::KeyedBuildResult> results;
    {
        nb::gil_scoped_release release;
        results = s.buildPathsWithResults(paths, buildMode, evalStore);
    }
    nb::list out;
    for (auto &kbr : results) out.append(nanopynix::build_result::from_kbr(kbr, s));
    return out;
}

// The direct binding. Takes derived paths for the same reason
// query_missing_paths does: the StorePath-only variant it replaces silently
// dropped the ^ output selector its own Python docstring advertised.
static nb::list build_paths_with_results(
        nix::Store &s,
        const nb::sequence &paths,
        nix::BuildMode buildMode = nix::bmNormal,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    return build_derived_paths_with_results(
        s,
        parse_derived_paths(s, derived_path_strings(paths, "build_paths_with_results"), "build_paths_with_results"),
        buildMode,
        evalStore);
}

static void copy_closure(
        nix::Store &s,
        const std::vector<nix::StorePath> &paths,
        std::shared_ptr<nix::Store> destStore,
        bool repair = false,
        bool checkSigs = true,
        bool substitute = false) {
    nix::StorePathSet pathSet(paths.begin(), paths.end());
    nb::gil_scoped_release release;
    nix::copyClosure(s, *destStore, pathSet,
        repair ? nix::Repair : nix::NoRepair,
        checkSigs ? nix::CheckSigs : nix::NoCheckSigs,
        substitute ? nix::Substitute : nix::NoSubstitute);
}

// One node of `Derivation::inputDrvs`, which is a `DerivedPathMap` -- a *tree*
// of `{V value; Map childMap;}`, nesting once per level of dynamic derivation.
//
// This used to be flattened in place with `dynamic_outputs[name] =
// *child.value.begin()`, which kept only the first output of each child and
// never looked at `child.childMap` at all, so anything deeper than one level
// was dropped without a trace. Templated on the node type because the map's
// value type differs across the supported Nix versions.
template<typename Node>
static nb::dict derived_path_node_to_dict(const Node &node) {
    nb::dict entry;
    nb::list outs;
    for (auto &output : node.value) outs.append(output);
    entry["outputs"] = outs;

    nb::dict dynamic_outputs;
    for (auto &[outputName, child] : node.childMap)
        dynamic_outputs[outputName.c_str()] = derived_path_node_to_dict(child);
    entry["dynamic_outputs"] = dynamic_outputs;
    return entry;
}

static nb::dict read_derivation(nix::Store &s, const nix::StorePath &drvPath) {
    nix::Derivation drv;
    {
        nb::gil_scoped_release release;
        drv = s.readDerivation(drvPath);
    }
    nb::dict d;
    d["name"] = drv.name;

    // outputs: map<string, DerivationOutput>
    nb::dict outputs;
    for (auto &[name, output] : drv.outputs) {
        nb::dict o;
        std::visit(nix::overloaded{
            [&](const nix::DerivationOutput::InputAddressed &ia) {
                o["type"] = "InputAddressed";
                o["path"] = s.printStorePath(ia.path);
            },
            [&](const nix::DerivationOutput::CAFixed &caf) {
                o["type"] = "CAFixed";
                o["ca"] = nix::renderContentAddress(nix::ContentAddress{caf.ca});
            },
            [&](const nix::DerivationOutput::CAFloating &caf) {
                o["type"] = "CAFloating";
                o["method"] = std::string(caf.method.render());
                o["hash_algo"] = std::string(nix::printHashAlgo(caf.hashAlgo));
            },
            [&](const nix::DerivationOutput::Deferred &) {
                o["type"] = "Deferred";
            },
            [&](const nix::DerivationOutput::Impure &imp) {
                o["type"] = "Impure";
                o["method"] = std::string(imp.method.render());
                o["hash_algo"] = std::string(nix::printHashAlgo(imp.hashAlgo));
            },
        }, output.raw);
        outputs[name.c_str()] = o;
    }
    d["outputs"] = outputs;

    // input_srcs: set<StorePath>
    nb::list input_srcs;
    for (auto &p : drv.inputSrcs) input_srcs.append(s.printStorePath(p));
    d["input_srcs"] = input_srcs;

    // input_drvs: map<drvPath, DerivationOutputs>
    nb::dict input_drvs;
    for (auto &[path, node] : drv.inputDrvs.map)
        input_drvs[s.printStorePath(path).c_str()] = derived_path_node_to_dict(node);
    d["input_drvs"] = input_drvs;

    d["system"] = drv.platform;
    d["builder"] = drv.builder;

    nb::list args;
    for (auto &a : drv.args) args.append(a);
    d["args"] = args;

    nb::dict env;
    for (auto &[k, v] : drv.env) env[k.c_str()] = v;
    d["env"] = env;

    // Structured attributes are NOT recoverable from `env`. Nix's ATerm parser
    // routes the `__json` attribute into `drv.structuredAttrs` and deliberately
    // does not insert it into `env` (see `derivations.cc`'s parse loop, and
    // `StructuredAttrs::tryExtract`, which erases the key). Reading only `env`
    // therefore dropped every attribute of a `__structuredAttrs = true`
    // derivation -- the common case in modern nixpkgs -- with nothing to
    // indicate anything was missing.
    //
    // `unparse()` is Nix's own serialiser for the field and returns the
    // `{"__json", payload}` pair, so this hands back exactly the bytes Nix read.
    if (drv.structuredAttrs)
        d["structured_attrs"] = drv.structuredAttrs->unparse().second;
    else
        d["structured_attrs"] = nb::none();

    return d;
}

// Rewrite a derivation so that its builder dumps its own environment, and
// write the rewrite back to the store. This is what `nix develop` and
// `nix print-dev-env` both start with -- `getDerivationEnvironment` in
// `src/nix/develop.cc` -- and this follows it step for step.
//
// **This lives in C++ because the three supported Nix versions disagree on
// the last two steps.** 2.31 writes with a free `writeDerivation` and fills
// the output paths by hand, with `hashDerivationModulo` and `makeOutputPath`,
// and it has a separate branch for `ca-derivations`. 2.34 and 2.35 have
// `Derivation::fillInOutputPaths` and a `writeDerivation` method. A Python
// caller would need four more bindings and a branch on the Nix version, and
// `AGENTS.md` keeps version branches out of the library.
//
// The `nix::Derivation` also never leaves C++ this way: `derivationFromPath`
// builds it, this mutates it, `writeDerivation` consumes it. So there is no
// dict to reconstruct on the way back, and no faithfulness to prove.
//
// `get_env_script` is a parameter rather than a constant compiled in here.
// Nix keeps its own copy as `getEnvSh`, a file-static string in the `nix`
// binary that is in no library, so a consumer carries its own. It arrives as
// text because the script has to reach the store *before* the derivation is
// hashed, which is why a caller cannot add it and pass a path.
//
// One store, where Nix takes two. Nix reads and writes the derivation with
// its `evalStore` and builds with the other. A caller that wants that split
// can open the evaluation store and call this on it.
static nix::StorePath write_dev_shell_derivation(
    nix::Store &s,
    const nix::StorePath &drv_path,
    const std::string &get_env_script) {
    std::optional<nix::StorePath> result;
    {
        nb::gil_scoped_release release;
        auto drv = s.derivationFromPath(drv_path);

        // The contract of the command, and the reason it can promise a shell
        // at all. Nix refuses here too, with this message.
        if (nix::baseNameOf(drv.builder) != "bash")
            throw nix::Error("'develop' only works on derivations that use 'bash' as their builder");

        nix::StringSource source{get_env_script};
        auto script_path = s.addToStoreFromDump(
            source,
            "get-env.sh",
            nix::FileSerialisationMethod::Flat,
            nix::ContentAddressMethod::Raw::Text,
            nix::HashAlgorithm::SHA256,
            {});

        drv.args = {s.printStorePath(script_path)};

        // Drop the derivation checks. A dev shell is not the build, so the
        // reference checks that the build declares must not apply to it.
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
        // 2.31 erases the flat variables and leaves `outputChecks` inside a
        // structured-attrs derivation alone. Matched rather than corrected,
        // so that `print-dev-env` agrees with the `nix` of the same version.
        drv.env.erase("allowedReferences");
        drv.env.erase("allowedRequisites");
        drv.env.erase("disallowedReferences");
        drv.env.erase("disallowedRequisites");
#else
        if (drv.structuredAttrs) {
            drv.structuredAttrs->structuredAttrs.erase("outputChecks");
        } else {
            drv.env.erase("allowedReferences");
            drv.env.erase("allowedRequisites");
            drv.env.erase("disallowedReferences");
            drv.env.erase("disallowedRequisites");
        }
#endif
        drv.env.erase("name");

        drv.name += "-env";
        drv.env.emplace("name", drv.name);
        drv.inputSrcs.insert(script_path);

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
        if (nix::experimentalFeatureSettings.isEnabled(nix::Xp::CaDerivations)) {
            for (auto &output : drv.outputs) {
                output.second = nix::DerivationOutput::Deferred{};
                drv.env[output.first] = nix::hashPlaceholder(output.first);
            }
        } else {
            for (auto &output : drv.outputs) {
                output.second = nix::DerivationOutput::Deferred{};
                drv.env[output.first] = "";
            }
            // `Store` still derives from `StoreDirConfig` on this version, so
            // `makeOutputPath` is on the store itself and not on `.config`.
            auto hashes_modulo = nix::hashDerivationModulo(s, drv, true);
            for (auto &output : drv.outputs) {
                auto out_path = s.makeOutputPath(output.first, hashes_modulo.hashes.at(output.first), drv.name);
                output.second = nix::DerivationOutput::InputAddressed{.path = out_path};
                drv.env[output.first] = s.printStorePath(out_path);
            }
        }
        result.emplace(nix::writeDerivation(s, drv));
#else
        // Only the two addressed kinds become deferred. CAFloating, Deferred
        // and Impure already have no path to invalidate.
        for (auto &[output_name, output] : drv.outputs) {
            std::visit(nix::overloaded{
                [&](const nix::DerivationOutput::InputAddressed &) {
                    output = nix::DerivationOutput::Deferred{};
                    drv.env[output_name] = "";
                },
                [&](const nix::DerivationOutput::CAFixed &) {
                    output = nix::DerivationOutput::Deferred{};
                    drv.env[output_name] = "";
                },
                [&](const auto &) {},
            }, output.raw);
        }
        drv.fillInOutputPaths(s);
        result.emplace(s.writeDerivation(drv));
#endif
    }
    return *result;
}

static std::string store_uri(nix::Store &s, bool with_params) {
    return with_params ? s.config.getReference().render() : s.config.getHumanReadableURI();
}

static nb::dict store_dirs_to_dict(nix::Store &s) {
    nb::dict d;
    d["store_dir"] = std::string(s.config.storeDir_);
    d["uri"] = s.config.getHumanReadableURI();
    d["root_dir"] = nb::none();
    d["state_dir"] = nb::none();
    d["log_dir"] = nb::none();
    d["real_store_dir"] = nb::none();
    d["build_dir"] = nb::none();

    if (auto *local_fs_store = dynamic_cast<nix::LocalFSStore *>(&s)) {
        auto &config = local_fs_store->config;
        auto root_dir = config.rootDir.get();
        if (root_dir)
            d["root_dir"] = nix_path_to_string(*root_dir);
        d["state_dir"] = nix_path_to_string(config.stateDir.get());
        d["log_dir"] = nix_path_to_string(config.logDir.get());
        d["real_store_dir"] = nix_path_to_string(config.realStoreDir.get());
    }

    if (auto *local_store = dynamic_cast<nix::LocalStore *>(&s)) {
        d["build_dir"] = nix_path_to_string(local_store->config->getBuildDir());
    }

    return d;
}

static nb::dict store_get_store_dirs_direct(nix::Store &s) {
    return store_dirs_to_dict(s);
}

static std::optional<std::string> get_build_log(nix::Store &s, const nix::StorePath &path) {
    {
        nb::gil_scoped_release release;
        return require_log_store(s).getBuildLog(path);
    }
}

static nix::StorePath add_to_store(
    nix::Store &s,
    const std::string &path,
    const std::optional<std::string> &name,
    const std::string &method,
    const std::string &hash_algo) {
    auto resolved_name = store_add_name(name, path);
    auto source_path = source_path_from(path);
    auto ca_method = parse_content_address_method(method);
    auto algo = parse_store_hash_algo(hash_algo);
    std::optional<nix::StorePath> result;
    {
        nb::gil_scoped_release release;
        result.emplace(s.addToStoreSlow(resolved_name, source_path, ca_method, algo, {}).path);
    }
    return *result;
}

static nix::StorePath compute_store_path(
    nix::Store &s,
    const std::string &path,
    const std::optional<std::string> &name,
    const std::string &method,
    const std::string &hash_algo) {
    auto resolved_name = store_add_name(name, path);
    auto source_path = source_path_from(path);
    auto ca_method = parse_content_address_method(method);
    auto algo = parse_store_hash_algo(hash_algo);
    std::optional<nix::StorePath> result;
    {
        nb::gil_scoped_release release;
        result.emplace(s.computeStorePath(resolved_name, source_path, ca_method, algo, {}).first);
    }
    return *result;
}

// The proto-shaped pair are adapters over the native ones above: unpack the
// request, hand back a printed path. They carry no store logic of their own.
// =========================================================================
// Store bindings
// =========================================================================

static void bind_store(nb::module_ &m) {
    nb::class_<nix::Store>(m, "Store")
        .def("close", &close_store, nb::call_guard<nb::gil_scoped_release>())
        // Query
        .def("get_store_dir",
             [](nix::Store &s) -> std::string { return s.config.storeDir_; })
        .def("get_store_dirs", &store_get_store_dirs_direct)
        .def("get_uri",
             &store_uri,
             "with_params"_a = false)
        .def("is_valid_path", [](nix::Store &s, const nix::StorePath &p) { return s.isValidPath(p); },
             nb::call_guard<nb::gil_scoped_release>(), "path"_a)
        // Deliberately does not absolutize a relative path: absolutization
        // now lives in LocalStore._store_path(), which both engines share, so
        // that the two cannot diverge over it the way they used to. The
        // empty-string guard stays here, as the last line of defence for a
        // raw-binding caller -- parseStorePath() aborts the process on "",
        // it does not throw.
        .def("parse_store_path",
             [](nix::Store &s, const std::string &p) {
                 if (p.empty())
                     throw nix::BadStorePath("parse_store_path: store path must not be empty");
                 return s.parseStorePath(p);
             },
             nb::call_guard<nb::gil_scoped_release>(), "path"_a)
        .def("follow_links_to_store_path",
             [](nix::Store &s, const std::string &p) { return s.followLinksToStorePath(p); },
             nb::call_guard<nb::gil_scoped_release>(), "path"_a)
        .def("get_build_log", &get_build_log, "path"_a)
        // Build. `nix::Store::buildPaths` had a binding here too, and it went:
        // no caller in any of the three packages, and no test. It also took
        // `StorePath`, which cannot carry a `^` selector, so its bare `.drv`
        // meant every output while the entry point beside it kept Nix's
        // `Opaque`. Two readings of one argument, in one file. Issue #74.
        //
        // What went with it is `buildPaths`'s throw-on-failure: this one
        // returns a `BuildResult` for each path and reports a failure there.
        // Bring the binding back with a test if a caller ever wants the throw.
        .def(
            "build_paths_with_results",
            &build_paths_with_results,
            "paths"_a,
            "build_mode"_a = nix::bmNormal,
            "eval_store"_a = nullptr)
        .def("read_derivation", &read_derivation, "drv_path"_a)
        .def("write_dev_shell_derivation", &write_dev_shell_derivation, "drv_path"_a, "get_env_script"_a)
        // Path info
        .def("query_path_info", &query_path_info, "path"_a)
        .def("dump_db", &dump_db, "paths"_a, "show_derivers"_a = true, "show_hash"_a = true)
        .def("query_path_from_hash_part",
             [](nix::Store &s, const std::string &h) { return s.queryPathFromHashPart(h); },
             nb::call_guard<nb::gil_scoped_release>(), "hash"_a)
        // Closures
        .def("compute_fs_closure", &compute_fs_closure,
             "path"_a, "flip_direction"_a = false, "include_outputs"_a = false, "include_derivers"_a = false)
        .def("copy_closure", &copy_closure,
             "paths"_a, "dest_store"_a, "repair"_a = false, "check_sigs"_a = true, "substitute"_a = false)
        .def("query_missing", &query_missing_paths, "paths"_a)
        // Derivations
        .def("query_derivation_outputs", &query_derivation_outputs, "path"_a)
        .def("query_valid_derivers", &query_valid_derivers, "path"_a)
        // Misc
        .def("query_all_valid_paths", &query_all_valid_paths)
        .def("query_referrers", &query_referrers, "path"_a)
        .def("query_substitutable_paths", &query_substitutable_paths, "paths"_a)
        // Content-addressed adds
        .def(
            "add_to_store",
            &add_to_store,
            "path"_a,
            "name"_a = nb::none(),
            "method"_a = "nar",
            "hash_algo"_a = "sha256")
        .def(
            "compute_store_path",
            &compute_store_path,
            "path"_a,
            "name"_a = nb::none(),
            "method"_a = "nar",
            "hash_algo"_a = "sha256")
        // GC
        .def("add_temp_root", [](nix::Store &s, const nix::StorePath &p) { s.addTempRoot(p); },
             nb::call_guard<nb::gil_scoped_release>(), "path"_a)
        .def("find_roots", &find_roots, "censor"_a = true)
        .def(
            "collect_garbage",
            &collect_garbage,
            "action"_a,
            "ignore_liveness"_a = false,
            "paths_to_delete"_a = std::vector<nix::StorePath>{},
            "max_freed"_a = std::numeric_limits<uint64_t>::max())
        .def(
            "add_perm_root",
            [](nix::Store &s, const nix::StorePath &store_path, const std::string &gc_root) {
                return nix_path_to_string(require_local_fs_store(s).addPermRoot(store_path, gc_root));
            },
            nb::call_guard<nb::gil_scoped_release>(),
            "store_path"_a,
            "gc_root"_a)
        .def(
            "add_indirect_root",
            [](nix::Store &s, const std::string &path) {
                require_indirect_root_store(s).addIndirectRoot(path);
            },
            nb::call_guard<nb::gil_scoped_release>(),
            "path"_a)
        .def("ensure_path", [](nix::Store &s, const nix::StorePath &p) { s.ensurePath(p); },
             nb::call_guard<nb::gil_scoped_release>(), "path"_a)
        .def("optimise_store", [](nix::Store &s) { s.optimiseStore(); },
             nb::call_guard<nb::gil_scoped_release>())
        .def(
            "verify_store",
            [](nix::Store &s, bool check_contents, bool repair) {
                return s.verifyStore(check_contents, repair ? nix::Repair : nix::NoRepair);
            },
            nb::call_guard<nb::gil_scoped_release>(),
            "check_contents"_a = false,
            "repair"_a = false)
        // Proto-shaped StoreService RPC entrypoints
;
}

// =========================================================================

void nanopynix_bind_store(nb::module_ &m) {
    m.doc() = "nanopynix: Nix store bindings (StorePath, Store, build)";

    nb::enum_<nix::BuildMode>(m, "BuildMode")
        .value("Normal", nix::BuildMode::bmNormal)
        .value("Repair", nix::BuildMode::bmRepair)
        .value("Check", nix::BuildMode::bmCheck);

    nb::enum_<NixGCAction>(m, "GCAction")
        .value("ReturnLive", gcReturnLive)
        .value("ReturnDead", gcReturnDead)
        .value("DeleteDead", gcDeleteDead)
        .value("DeleteSpecific", gcDeleteSpecific);

    m.def("open_store", &open_store_uri, "uri"_a);
    m.def("open_store", &open_store_default);
    m.def(
        "process_connection", &process_connection, nb::call_guard<nb::gil_scoped_release>(),
        "store"_a, "fd"_a, "trusted"_a, "recursive"_a = false,
        "Handle one Nix daemon-protocol connection on fd. The caller owns fd and must explicitly "
        "decide whether its peer is trusted.");
    m.def("register_store_implementation", &register_python_store,
          "name"_a, "doc"_a, "schemes"_a, "factory"_a,
          "Register a Python-backed store implementation.\n"
          "\n"
          "`factory.open_store()` is called once per `open_store('<scheme>://...')` "
          "and must return an instance of a `nanopynix.StoreImpl` subclass; "
          "anything else raises TypeError. Registration is global to the process "
          "and cannot be undone, so a name or scheme can only be claimed once.\n"
          "\n"
          "See `nanopynix.StoreImpl` for the operations a store may override, the "
          "shape of the reply `query_path_info` returns, and the `underlying_store` "
          "attribute that delegates everything a subclass does not implement.");

    // The operations `PyStoreImpl` can dispatch into Python, exported so that
    // `nanopynix.StoreImpl` derives its method list from this one rather than
    // repeating it. A method declared there but absent here would silently
    // never be called; deriving makes that unrepresentable.
    {
        nb::list dispatch_methods;
#define NANOPYNIX_STORE_DISPATCH_NAME(name) dispatch_methods.append(#name);
        NANOPYNIX_STORE_DISPATCH_METHODS(NANOPYNIX_STORE_DISPATCH_NAME)
#undef NANOPYNIX_STORE_DISPATCH_NAME
        PyObject *as_tuple = PySequence_Tuple(dispatch_methods.ptr());
        if (!as_tuple) throw nb::python_error();
        m.attr("STORE_DISPATCH_METHODS") = nb::steal<nb::tuple>(as_tuple);
    }

    bind_store_path(m);
    bind_store(m);

    // ── Pure utility functions (no init required) ───────────────
    m.def("compare_versions", [](const std::string &v1, const std::string &v2) -> int {
            auto ord = nix::compareVersions(v1, v2);
            if (ord < 0) return -1;
            if (ord > 0) return 1;
            return 0;
          },
          "v1"_a, "v2"_a,
          "Compare two Nix version strings. Returns -1, 0, or 1.");
    m.def("check_name", [](const std::string &name) {
            nix::checkName(name);
            return true;
          },
          "name"_a,
          "Validate a store path name component. Returns True or raises BadStorePath.");
    m.def("parse_store_reference", [](const std::string &uri) {
            auto ref = nix::StoreReference::parse(uri);
            nb::dict d;
            std::visit(nix::overloaded{
                [&](const nix::StoreReference::Auto &) {
                    d["type"] = "Auto";
                    d["scheme"] = nb::none();
                    d["authority"] = nb::none();
                },
                [&](const nix::StoreReference::Daemon &) {
                    d["type"] = "Daemon";
                    d["scheme"] = "unix";
                    d["authority"] = "";
                },
                [&](const nix::StoreReference::Local &) {
                    d["type"] = "Local";
                    d["scheme"] = "local";
                    d["authority"] = "";
                },
                [&](const nix::StoreReference::Specified &s) {
                    d["type"] = "Specified";
                    d["scheme"] = s.scheme;
                    d["authority"] = s.authority;
                },
            }, ref.variant);
            nb::dict params;
            for (auto &[key, value] : ref.params) params[key.c_str()] = value;
            d["params"] = params;
            d["render"] = ref.render(true);
            d["render_without_params"] = ref.render(false);
            return d;
          },
          "uri"_a,
          "Parse a Nix store URI into its components (nix::StoreReference::parse), without "
          "opening a store. Returns a dict with 'type' (one of 'Auto', 'Specified', 'Daemon', "
          "'Local'), 'scheme'/'authority' (None for Auto), 'params', and both 'render' "
          "(with params) and 'render_without_params'.\n\n"
          "This is pure string parsing and does NOT reproduce Store.get_uri()'s collapsing: "
          "parsing only resolves to Daemon/Local when the input literally says 'daemon'/'local'. "
          "An already-open store's get_uri() instead compares its live socket path against Nix's "
          "*current* default daemon socket (settings.nixDaemonSocketFile) and collapses a "
          "unix://<path> URI to the bare 'daemon' shorthand whenever they match -- deliberately, "
          "per nix::UDSRemoteStoreConfig::getReference()'s own comment, for compatibility with "
          "older tooling that chokes on 'unix://'. So the same connection can render differently "
          "depending on the process-wide default socket path at the time it was opened, "
          "independent of the URI string originally used to connect it.");
    m.def("list_store_types_json", []() {
            auto res = nlohmann::json::object();
            for (auto &[name, implem] : nix::Implementations::registered()) {
                auto &entry = res[name];
                entry["doc"] = implem.doc;
                entry["uri-schemes"] = implem.uriSchemes;
                // getConfig() builds the config with no params, so every value
                // here is the default rather than anything a URI asked for.
                entry["settings"] = implem.getConfig()->toJSON();
                if (implem.experimentalFeature)
                    entry["experimentalFeature"] =
                        std::string(nix::showExperimentalFeature(*implem.experimentalFeature));
                else
                    entry["experimentalFeature"] = nullptr;
            }
            return res.dump();
          },
          "Return every registered store type as JSON, keyed by type name.\n\n"
          "Each entry has 'doc', 'uri-schemes', 'settings' (the same metadata shape as "
          "list_settings_metadata_json, one entry per setting the type accepts as a URI "
          "query parameter), and 'experimentalFeature' (the feature gating the type, or "
          "None).\n\n"
          "The registry is populated by static initializers, one per linked store "
          "implementation, so this reports what THIS build can actually open rather than "
          "what Nix documents. Use it to check a typed store model against the Nix it is "
          "linked with.");
    m.def("render_store_reference", [](const std::string &uri, bool with_params) {
            return nix::StoreReference::parse(uri).render(with_params);
          },
          "uri"_a,
          "with_params"_a = true,
          "Parse and re-render a Nix store URI (nix::StoreReference::parse(uri).render()). "
          "String in, string out -- a normalization/round-trip, not a structural parse. Use "
          "parse_store_reference() for the individual components. Like parse_store_reference, "
          "this never applies the open-store 'daemon' shorthand collapsing that Store.get_uri() "
          "can apply -- see that function's docstring for why.");

    // ── Exception bindings ──────────────────────────────────────
    // Moved to nix_errors.cpp, which owns every Nix exception class and the
    // single translator that dispatches them. Nothing to register here.
}
