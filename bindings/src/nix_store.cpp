#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/typing.h>

#include <nix/store/store-api.hh>
#include <nix/store/store-open.hh>
#include <nix/store/path.hh>
#include <nix/store/derived-path.hh>
#include <nix/store/build-result.hh>
#include <nix/store/daemon.hh>
#include <nix/store/path-info.hh>
#include <nix/store/derivations.hh>
#include <nix/store/content-address.hh>
#include <nlohmann/json.hpp>
#include <nix/util/hash.hh>
#include <nix/util/serialise.hh>
#include <nix/util/file-descriptor.hh>
#include <nix/util/error.hh>

#include "py_store_impl.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// Helpers — convert Nix types to nb::dict for Pydantic validation
// =========================================================================

static nb::dict store_path_to_dict(const std::string &to_string) {
    nb::dict d;
    d["base_name"] = to_string;
    return d;
}

static nb::dict store_path_to_dict(const nix::StorePath &sp) {
    return store_path_to_dict(std::string(sp.to_string()));
}

static nb::list store_paths_to_dict_list(const nix::StorePathSet &paths) {
    nb::list result;
    for (auto &p : paths) result.append(store_path_to_dict(p));
    return result;
}

static std::string build_success_status_str(nix::BuildResult::Success::Status s) {
    using enum nix::BuildResult::Success::Status;
    switch (s) {
        case Built:                  return "built";
        case Substituted:            return "substituted";
        case AlreadyValid:           return "already-valid";
        case ResolvesToAlreadyValid: return "resolves-to-already-valid";
    }
    return "unknown";
}

static std::string build_failure_status_str(nix::BuildResult::Failure::Status s) {
    using enum nix::BuildResult::Failure::Status;
    switch (s) {
        case PermanentFailure:  return "permanent-failure";
        case InputRejected:     return "input-rejected";
        case OutputRejected:    return "output-rejected";
        case TransientFailure:  return "transient-failure";
        case CachedFailure:     return "cached-failure";
        case TimedOut:          return "timed-out";
        case MiscFailure:       return "misc-failure";
        case DependencyFailed:  return "dependency-failed";
        case LogLimitExceeded:  return "log-limit-exceeded";
        case NotDeterministic:  return "not-deterministic";
        case NoSubstituters:    return "no-substituters";
        case HashMismatch:      return "hash-mismatch";
    }
    return "unknown";
}

static nb::dict build_result_to_dict(const std::string &drv_path, bool success,
                                      const std::string &status, const std::string &error_msg) {
    nb::dict d;
    d["drv_path"] = drv_path;
    d["success"] = success;
    d["status"] = status;
    d["error_msg"] = error_msg;
    return d;
}

static nb::dict build_result_from_kbr(const nix::KeyedBuildResult &kbr,
                                       const nix::StoreDirConfig &store) {
    auto path = kbr.path.to_string(store);
    if (auto *success = kbr.tryGetSuccess())
        return build_result_to_dict(path, true, build_success_status_str(success->status), "");
    if (auto *failure = kbr.tryGetFailure())
        return build_result_to_dict(path, false, build_failure_status_str(failure->status), failure->msg());
    return build_result_to_dict(path, false, "unknown", "");
}

static nb::dict build_result_from_br(const nix::BuildResult &br) {
    if (auto *success = br.tryGetSuccess())
        return build_result_to_dict("", true, build_success_status_str(success->status), "");
    if (auto *failure = br.tryGetFailure())
        return build_result_to_dict("", false, build_failure_status_str(failure->status), failure->msg());
    return build_result_to_dict("", false, "unknown", "");
}

static nb::dict path_info_to_dict(const nix::ValidPathInfo &info) {
    nb::dict d;
    d["path"] = store_path_to_dict(info.path);

    // references — list of store path dicts
    nb::list refs;
    for (auto &r : info.references) refs.append(store_path_to_dict(r));
    d["references"] = refs;

    d["nar_hash"] = info.narHash.to_string(nix::HashFormat::SRI, true);
    d["nar_size"] = nb::int_(info.narSize);

    if (info.registrationTime)
        d["registration_time"] = nb::int_(info.registrationTime);
    else
        d["registration_time"] = nb::none();

    if (info.deriver)
        d["deriver"] = store_path_to_dict(*info.deriver);
    else
        d["deriver"] = nb::none();

    if (info.ca)
        d["ca"] = nix::renderContentAddress(*info.ca);
    else
        d["ca"] = nb::none();

    d["ultimate"] = info.ultimate;
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
    return nix::openStore(uri).get_ptr();
}
static std::shared_ptr<nix::Store> open_store_default() {
    return nix::openStore().get_ptr();
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

static std::string request_string(const nb::dict &request, const char *key) {
    return nb::cast<std::string>(request[nb::str(key)]);
}

static bool request_bool(const nb::dict &request, const char *key) {
    return nb::cast<bool>(request[nb::str(key)]);
}

static nix::StorePath request_store_path(nix::Store &s, const nb::dict &request, const char *key) {
    auto path = request_string(request, key);
    if (!path.empty() && path[0] != '/') path = s.config.storeDir_ + "/" + path;
    return s.parseStorePath(path);
}

static std::vector<nix::StorePath> request_store_paths(nix::Store &s, const nb::dict &request, const char *key) {
    std::vector<std::string> raw = nb::cast<std::vector<std::string>>(request[nb::str(key)]);
    std::vector<nix::StorePath> paths;
    paths.reserve(raw.size());
    for (auto &path : raw) {
        if (!path.empty() && path[0] != '/') path = s.config.storeDir_ + "/" + path;
        paths.push_back(s.parseStorePath(path));
    }
    return paths;
}

// --- PathInfo ---

static nb::dict query_path_info(nix::Store &s, const nix::StorePath &path) {
    auto info = s.queryPathInfo(path);
    return path_info_to_dict(*info);
}

// --- Closures ---

static nb::list compute_fs_closure(nix::Store &s, const nix::StorePath &path,
                                    bool flip, bool include_outputs, bool include_derivers) {
    nix::StorePathSet out;
    s.computeFSClosure(path, out, flip, include_outputs, include_derivers);
    return store_paths_to_dict_list(out);
}

// --- MissingInfo ---

static nb::dict query_missing(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(nix::DerivedPath{nix::DerivedPath::Opaque{p}});
    auto m = s.queryMissing(dps);
    nb::dict d;
    d["will_build"] = store_paths_to_dict_list(m.willBuild);
    d["will_substitute"] = store_paths_to_dict_list(m.willSubstitute);
    d["unknown"] = store_paths_to_dict_list(m.unknown);
    d["download_size"] = nb::int_(m.downloadSize);
    d["nar_size"] = nb::int_(m.narSize);
    return d;
}

// --- Collective queries ---

static nb::list query_derivation_outputs(nix::Store &s, const nix::StorePath &path) {
    return store_paths_to_dict_list(s.queryDerivationOutputs(path));
}
static nb::list query_valid_derivers(nix::Store &s, const nix::StorePath &path) {
    return store_paths_to_dict_list(s.queryValidDerivers(path));
}
static nb::list query_all_valid_paths(nix::Store &s) {
    return store_paths_to_dict_list(s.queryAllValidPaths());
}
static nb::list query_referrers(nix::Store &s, const nix::StorePath &path) {
    nix::StorePathSet refs;
    s.queryReferrers(path, refs);
    return store_paths_to_dict_list(refs);
}
static nb::list query_substitutable_paths(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::StorePathSet ps(paths.begin(), paths.end());
    auto subs = s.querySubstitutablePaths(ps);
    return store_paths_to_dict_list(subs);
}

// --- Build ---

static nb::list build_paths_with_results(
        nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(nix::DerivedPath{nix::DerivedPath::Opaque{p}});
    auto results = s.buildPathsWithResults(dps);
    nb::list out;
    for (auto &kbr : results) out.append(build_result_from_kbr(kbr, s));
    return out;
}

static void build_paths(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(nix::DerivedPath{nix::DerivedPath::Opaque{p}});
    s.buildPaths(dps);
}

static nb::dict read_derivation(nix::Store &s, const nix::StorePath &drvPath) {
    auto drv = s.readDerivation(drvPath);
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
                o["hashAlgo"] = std::string(nix::printHashAlgo(caf.hashAlgo));
            },
            [&](const nix::DerivationOutput::Deferred &) {
                o["type"] = "Deferred";
            },
            [&](const nix::DerivationOutput::Impure &imp) {
                o["type"] = "Impure";
                o["method"] = std::string(imp.method.render());
                o["hashAlgo"] = std::string(nix::printHashAlgo(imp.hashAlgo));
            },
        }, output.raw);
        outputs[name.c_str()] = o;
    }
    d["outputs"] = outputs;

    // inputSrcs: set<StorePath>
    nb::list inputSrcs;
    for (auto &p : drv.inputSrcs) inputSrcs.append(s.printStorePath(p));
    d["inputSrcs"] = inputSrcs;

    // inputDrvs: DerivedPathMap<set<OutputName>>
    nb::list inputDrvs;
    bool has_dynamic = false;
    for (auto &[path, node] : drv.inputDrvs.map) {
        nb::dict entry;
        entry["path"] = s.printStorePath(path);
        nb::list outs;
        for (auto &o : node.value) outs.append(o);
        entry["outputs"] = outs;
        // Nested paths → dynamic derivations
        nb::dict children;
        for (auto &[outputName, child] : node.childMap) {
            has_dynamic = true;
            nb::list childOuts;
            for (auto &o : child.value) childOuts.append(o);
            children[outputName.c_str()] = childOuts;
        }
        if (!children.empty()) entry["children"] = children;
        inputDrvs.append(entry);
    }
    d["inputDrvs"] = inputDrvs;
    d["has_dynamic_inputs"] = has_dynamic;

    d["platform"] = drv.platform;
    d["builder"] = drv.builder;

    nb::list args;
    for (auto &a : drv.args) args.append(a);
    d["args"] = args;

    nb::list env;
    for (auto &[k, v] : drv.env) env.append(nb::make_tuple(k, v));
    d["env"] = env;

    if (drv.structuredAttrs)
        d["structuredAttrs"] = nlohmann::json(drv.structuredAttrs->structuredAttrs).dump();
    else
        d["structuredAttrs"] = nb::none();

    auto dtype = drv.type();
    d["is_ca"] = dtype.isCA();
    d["has_known_output_paths"] = dtype.hasKnownOutputPaths();

    return d;
}

static nb::dict build_derivation(nix::Store &s, const nix::StorePath &drvPath,
                                  nix::BuildMode buildMode) {
    auto drv = s.readDerivation(drvPath);
    auto result = s.buildDerivation(drvPath,
        static_cast<const nix::BasicDerivation &>(drv), buildMode);
    return build_result_from_br(result);
}

static nb::dict store_get_uri(nix::Store &s, const nb::dict &) {
    nb::dict d;
    d["uri"] = s.config.getHumanReadableURI();
    return d;
}

static nb::dict store_get_store_dir(nix::Store &s, const nb::dict &) {
    nb::dict d;
        d["dir"] = std::string(s.config.storeDir_);
    return d;
}

static nb::dict store_is_valid_path(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["valid"] = s.isValidPath(request_store_path(s, request, "path"));
    return d;
}

static nb::dict store_parse_store_path(nix::Store &s, const nb::dict &request) {
    return store_path_to_dict(request_store_path(s, request, "path"));
}

static nb::dict store_query_path_info(nix::Store &s, const nb::dict &request) {
    return query_path_info(s, request_store_path(s, request, "path"));
}

static nb::dict store_query_path_from_hash_part(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    auto path = s.queryPathFromHashPart(request_string(request, "hash_part"));
    if (path) d["path"] = store_path_to_dict(*path);
    else d["path"] = nb::none();
    return d;
}

static nb::dict store_compute_fs_closure(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = compute_fs_closure(
        s,
        request_store_path(s, request, "path"),
        request_bool(request, "flip_direction"),
        request_bool(request, "include_outputs"),
        request_bool(request, "include_derivers"));
    return d;
}

static nb::dict store_query_missing(nix::Store &s, const nb::dict &request) {
    return query_missing(s, request_store_paths(s, request, "paths"));
}

static nb::dict store_query_derivation_outputs(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_derivation_outputs(s, request_store_path(s, request, "path"));
    return d;
}

static nb::dict store_query_valid_derivers(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_valid_derivers(s, request_store_path(s, request, "path"));
    return d;
}

static nb::dict store_query_all_valid_paths(nix::Store &s, const nb::dict &) {
    nb::dict d;
    d["paths"] = query_all_valid_paths(s);
    return d;
}

static nb::dict store_query_referrers(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_referrers(s, request_store_path(s, request, "path"));
    return d;
}

static nb::dict store_query_substitutable_paths(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_substitutable_paths(s, request_store_paths(s, request, "paths"));
    return d;
}

static nb::dict store_build_paths_with_results(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["results"] = build_paths_with_results(s, request_store_paths(s, request, "paths"));
    return d;
}

static nb::dict store_read_derivation(nix::Store &s, const nb::dict &request) {
    return read_derivation(s, request_store_path(s, request, "path"));
}

static nb::dict store_build_derivation(nix::Store &s, const nb::dict &request) {
    return build_derivation(
        s,
        request_store_path(s, request, "path"),
        static_cast<nix::BuildMode>(nb::cast<int>(request[nb::str("build_mode")])));
}

static nb::dict store_follow_links_to_store_path(
        nix::Store &s, const nb::dict &request) {
    return store_path_to_dict(s.followLinksToStorePath(request_string(request, "path")));
}

static nb::dict store_add_temp_root(nix::Store &s, const nb::dict &request) {
    s.addTempRoot(request_store_path(s, request, "path"));
    return nb::dict();
}

// =========================================================================
// Store bindings
// =========================================================================

static void bind_store(nb::module_ &m) {
    nb::class_<nix::Store>(m, "Store")
        // Query
        .def("get_store_dir",
             [](nix::Store &s) -> std::string { return s.config.storeDir_; })
        .def("get_uri",
             [](nix::Store &s) -> std::string { return s.config.getHumanReadableURI(); })
        .def("is_valid_path", [](nix::Store &s, const nix::StorePath &p) { return s.isValidPath(p); }, "path"_a)
        .def("parse_store_path", [](nix::Store &s, const std::string &p) { return s.parseStorePath(p); }, "path"_a)
        .def("follow_links_to_store_path",
             [](nix::Store &s, const std::string &p) { return s.followLinksToStorePath(p); }, "path"_a)
        // Build
        .def("build_paths", &build_paths, "paths"_a)
        .def("build_paths_with_results", &build_paths_with_results, "paths"_a)
        .def("read_derivation", &read_derivation, "drv_path"_a)
        .def("build_derivation", &build_derivation, "drv_path"_a, "build_mode"_a)
        // Path info
        .def("query_path_info", &query_path_info, "path"_a)
        .def("query_path_from_hash_part",
             [](nix::Store &s, const std::string &h) { return s.queryPathFromHashPart(h); }, "hash"_a)
        // Closures
        .def("compute_fs_closure", &compute_fs_closure,
             "path"_a, "flip_direction"_a = false, "include_outputs"_a = false, "include_derivers"_a = false)
        .def("query_missing", &query_missing, "paths"_a)
        // Derivations
        .def("query_derivation_outputs", &query_derivation_outputs, "path"_a)
        .def("query_valid_derivers", &query_valid_derivers, "path"_a)
        // Misc
        .def("query_all_valid_paths", &query_all_valid_paths)
        .def("query_referrers", &query_referrers, "path"_a)
        .def("query_substitutable_paths", &query_substitutable_paths, "paths"_a)
        // GC
        .def("add_temp_root", [](nix::Store &s, const nix::StorePath &p) { s.addTempRoot(p); }, "path"_a)
        // Proto-shaped StoreService RPC entrypoints
        .def("store_get_uri", &store_get_uri, "request"_a)
        .def("store_get_store_dir", &store_get_store_dir, "request"_a)
        .def("store_is_valid_path", &store_is_valid_path, "request"_a)
        .def("store_parse_store_path", &store_parse_store_path, "request"_a)
        .def("store_query_path_info", &store_query_path_info, "request"_a)
        .def("store_query_path_from_hash_part", &store_query_path_from_hash_part, "request"_a)
        .def("store_compute_fs_closure", &store_compute_fs_closure, "request"_a)
        .def("store_query_missing", &store_query_missing, "request"_a)
        .def("store_query_derivation_outputs", &store_query_derivation_outputs, "request"_a)
        .def("store_query_valid_derivers", &store_query_valid_derivers, "request"_a)
        .def("store_query_all_valid_paths", &store_query_all_valid_paths, "request"_a)
        .def("store_query_referrers", &store_query_referrers, "request"_a)
        .def("store_query_substitutable_paths", &store_query_substitutable_paths, "request"_a)
        .def("store_build_paths_with_results", &store_build_paths_with_results, "request"_a)
        .def("store_read_derivation", &store_read_derivation, "request"_a)
        .def("store_build_derivation", &store_build_derivation, "request"_a)
        .def("store_follow_links_to_store_path", &store_follow_links_to_store_path, "request"_a)
        .def("store_add_temp_root", &store_add_temp_root, "request"_a);
}

// =========================================================================

NB_MODULE(nanopynix_store, m) {
    m.doc() = "nanopynix: Nix store bindings (StorePath, Store, build)";

    nb::enum_<nix::BuildMode>(m, "BuildMode")
        .value("Normal", nix::BuildMode::bmNormal)
        .value("Repair", nix::BuildMode::bmRepair)
        .value("Check", nix::BuildMode::bmCheck);

    m.def("open_store", &open_store_uri, "uri"_a);
    m.def("open_store", &open_store_default);
    m.def("process_connection", &process_connection,
          "store"_a, "fd"_a, "trusted"_a = true, "recursive"_a = false,
          "Handle a single daemon client connection on the given fd.");
    m.def("register_store_implementation", &register_python_store,
          "name"_a, "doc"_a, "schemes"_a, "factory"_a,
          "Register a Python-backed store implementation.");

    bind_store_path(m);
    bind_store(m);

    // ── Exception bindings ──────────────────────────────────────
    nb::exception<nix::InvalidPath> py_invalid_path(m, "InvalidPath", PyExc_RuntimeError);
    nb::exception<nix::Unsupported> py_unsupported(m, "Unsupported", PyExc_RuntimeError);
    nb::exception<nix::BadStorePath> py_bad_sp(m, "BadStorePath", PyExc_RuntimeError);
    (void) py_invalid_path;
    (void) py_unsupported;
    (void) py_bad_sp;
}
