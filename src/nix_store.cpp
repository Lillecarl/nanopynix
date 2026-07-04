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

// Typed return types for stub generation
using StorePathList = nb::typed<nb::list, nix::StorePath>;

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
// BuildResult
// =========================================================================

struct PyBuildResult {
    std::string drv_path;
    bool success;
    std::string status;
    std::string error_msg;
};

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

static PyBuildResult build_result_from_kbr(const nix::KeyedBuildResult &kbr,
                                           const nix::StoreDirConfig &store) {
    PyBuildResult r;
    r.drv_path = kbr.path.to_string(store);
    if (auto *success = kbr.tryGetSuccess()) {
        r.success = true;
        r.status = build_success_status_str(success->status);
    } else if (auto *failure = kbr.tryGetFailure()) {
        r.success = false;
        r.status = build_failure_status_str(failure->status);
        r.error_msg = failure->msg();
    }
    return r;
}

static PyBuildResult build_result_from_br(const nix::BuildResult &br) {
    PyBuildResult r;
    if (auto *success = br.tryGetSuccess()) {
        r.success = true;
        r.status = build_success_status_str(success->status);
    } else if (auto *failure = br.tryGetFailure()) {
        r.success = false;
        r.status = build_failure_status_str(failure->status);
        r.error_msg = failure->msg();
    }
    return r;
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

// --- PathInfo ---

struct PyPathInfo {
    std::shared_ptr<const nix::ValidPathInfo> info;

    PyPathInfo(std::shared_ptr<const nix::ValidPathInfo> i) : info(std::move(i)) {}

    nix::StorePath path() const { return info->path; }
    std::string nar_hash() const { return info->narHash.to_string(nix::HashFormat::SRI, true); }
    uint64_t nar_size() const { return info->narSize; }
    std::optional<uint64_t> registration_time() const {
        if (info->registrationTime) return info->registrationTime;
        return std::nullopt;
    }
    std::optional<nix::StorePath> deriver() const {
        if (info->deriver) return *info->deriver;
        return std::nullopt;
    }
    StorePathList references() const {
        nb::list refs;
        for (auto &r : info->references)
            refs.append(nb::str(std::string(r.to_string()).c_str()));
        return refs;
    }
    std::optional<std::string> ca() const {
        if (info->ca) return nix::renderContentAddress(*info->ca);
        return std::nullopt;
    }
    bool ultimate() const { return info->ultimate; }
};

static PyPathInfo query_path_info(nix::Store &s, const nix::StorePath &path) {
    auto info = s.queryPathInfo(path);
    return PyPathInfo(info.get_ptr());
}

// --- Closures (needs conversion) ---

static StorePathList compute_fs_closure(nix::Store &s, const nix::StorePath &path,
                                    bool flip, bool include_outputs, bool include_derivers) {
    nix::StorePathSet out;
    s.computeFSClosure(path, out, flip, include_outputs, include_derivers);
    nb::list result;
    for (auto &p : out) result.append(nb::str(std::string(p.to_string()).c_str()));
    return result;
}

// --- MissingInfo ---

struct PyMissingInfo {
    StorePathList willBuild;
    StorePathList willSubstitute;
    StorePathList unknown;
    uint64_t downloadSize;
    uint64_t narSize;
};

static PyMissingInfo query_missing(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(nix::DerivedPath{nix::DerivedPath::Opaque{p}});
    auto m = s.queryMissing(dps);
    PyMissingInfo result;
    auto fill = [](auto &set) {
        nb::list l;
        for (auto &p : set) l.append(nb::str(std::string(p.to_string()).c_str()));
        return l;
    };
    result.willBuild = fill(m.willBuild);
    result.willSubstitute = fill(m.willSubstitute);
    result.unknown = fill(m.unknown);
    result.downloadSize = m.downloadSize;
    result.narSize = m.narSize;
    return result;
}

// --- Collective queries (need conversion) ---

template<typename F>
static StorePathList collect_store_paths(F &&f) {
    nb::list result;
    for (auto &p : f()) result.append(nb::str(std::string(p.to_string()).c_str()));
    return result;
}

static StorePathList query_derivation_outputs(nix::Store &s, const nix::StorePath &path) {
    return collect_store_paths([&]{ return s.queryDerivationOutputs(path); });
}
static StorePathList query_valid_derivers(nix::Store &s, const nix::StorePath &path) {
    return collect_store_paths([&]{ return s.queryValidDerivers(path); });
}
static StorePathList query_all_valid_paths(nix::Store &s) {
    return collect_store_paths([&]{ return s.queryAllValidPaths(); });
}
static StorePathList query_referrers(nix::Store &s, const nix::StorePath &path) {
    nix::StorePathSet refs;
    s.queryReferrers(path, refs);
    nb::list result;
    for (auto &p : refs) result.append(nb::str(std::string(p.to_string()).c_str()));
    return result;
}
static StorePathList query_substitutable_paths(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::StorePathSet ps(paths.begin(), paths.end());
    auto subs = s.querySubstitutablePaths(ps);
    nb::list result;
    for (auto &p : subs) result.append(nb::str(std::string(p.to_string()).c_str()));
    return result;
}

// --- Build (needs DerivedPath conversion) ---

static std::vector<PyBuildResult> build_paths_with_results(
        nix::Store &s, const std::vector<nix::StorePath> &paths,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(nix::DerivedPath{nix::DerivedPath::Opaque{p}});
    auto results = evalStore
        ? s.buildPathsWithResults(dps, nix::bmNormal, evalStore)
        : s.buildPathsWithResults(dps);
    std::vector<PyBuildResult> out;
    for (auto &kbr : results) out.push_back(build_result_from_kbr(kbr, s));
    return out;
}

static void build_paths(nix::Store &s, const std::vector<nix::StorePath> &paths,
                         std::shared_ptr<nix::Store> evalStore = nullptr) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(nix::DerivedPath{nix::DerivedPath::Opaque{p}});
    if (evalStore)
        s.buildPaths(dps, nix::bmNormal, evalStore);
    else
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

static PyBuildResult build_derivation(nix::Store &s, const nix::StorePath &drvPath,
                                       nix::BuildMode buildMode) {
    auto drv = s.readDerivation(drvPath);
    auto result = s.buildDerivation(drvPath,
        static_cast<const nix::BasicDerivation &>(drv), buildMode);
    return build_result_from_br(result);
}

// =========================================================================
// Bindings
// =========================================================================

static void bind_path_info(nb::module_ &m) {
    nb::class_<PyPathInfo>(m, "PathInfo")
        .def_prop_ro("path", &PyPathInfo::path)
        .def_prop_ro("nar_hash", &PyPathInfo::nar_hash)
        .def_prop_ro("nar_size", &PyPathInfo::nar_size)
        .def_prop_ro("registration_time", &PyPathInfo::registration_time)
        .def_prop_ro("deriver", &PyPathInfo::deriver)
        .def_prop_ro("references", &PyPathInfo::references)
        .def_prop_ro("ca", &PyPathInfo::ca)
        .def_prop_ro("ultimate", &PyPathInfo::ultimate)
        .def("__repr__", [](const PyPathInfo &p) {
            return "PathInfo('" + std::string(p.info->path.to_string()) + "')";
        });
}

static void bind_missing_info(nb::module_ &m) {
    nb::class_<PyMissingInfo>(m, "MissingInfo")
        .def_prop_ro("will_build", [](const PyMissingInfo &i) { return i.willBuild; })
        .def_prop_ro("will_substitute", [](const PyMissingInfo &i) { return i.willSubstitute; })
        .def_prop_ro("unknown", [](const PyMissingInfo &i) { return i.unknown; })
        .def_prop_ro("download_size", [](const PyMissingInfo &i) { return i.downloadSize; })
        .def_prop_ro("nar_size", [](const PyMissingInfo &i) { return i.narSize; });
}

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
        .def("build_paths", &build_paths, "paths"_a, "eval_store"_a = nb::none())
        .def("build_paths_with_results", &build_paths_with_results, "paths"_a, "eval_store"_a = nb::none())
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
        .def("add_temp_root", [](nix::Store &s, const nix::StorePath &p) { s.addTempRoot(p); }, "path"_a);
}

static void bind_build_result(nb::module_ &m) {
    nb::class_<PyBuildResult>(m, "BuildResult")
        .def_ro("drv_path", &PyBuildResult::drv_path)
        .def_ro("success", &PyBuildResult::success)
        .def_ro("status", &PyBuildResult::status)
        .def_ro("error_msg", &PyBuildResult::error_msg)
        .def("__repr__", [](const PyBuildResult &r) -> std::string {
            if (r.success)
                return "BuildResult(success=True, status='" + r.status + "')";
            return "BuildResult(success=False, status='" + r.status + "', error='" + r.error_msg + "')";
        });
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
    bind_path_info(m);
    bind_missing_info(m);
    bind_store(m);
    bind_build_result(m);

    // ── Exception bindings ──────────────────────────────────────
    nb::exception<nix::InvalidPath> py_invalid_path(m, "InvalidPath", PyExc_RuntimeError);
    nb::exception<nix::Unsupported> py_unsupported(m, "Unsupported", PyExc_RuntimeError);
    nb::exception<nix::BadStorePath> py_bad_sp(m, "BadStorePath", PyExc_RuntimeError);
    (void) py_invalid_path;
    (void) py_unsupported;
    (void) py_bad_sp;
}
