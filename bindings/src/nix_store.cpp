#include <nanobind/nanobind.h>
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
#include <nix/store/log-store.hh>
#include <nix/store/names.hh>
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
#include <nix/util/file-system.hh>
#include <nix/util/posix-source-accessor.hh>

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

static nb::list string_set_to_list(const nix::StringSet &values) {
    nb::list result;
    for (auto &value : values) result.append(value);
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

static uint64_t request_uint64(const nb::dict &request, const char *key) {
    return nb::cast<uint64_t>(request[nb::str(key)]);
}

static std::optional<std::string> request_optional_string(const nb::dict &request, const char *key) {
    auto value = request[nb::str(key)];
    if (value.is_none())
        return std::nullopt;
    return nb::cast<std::string>(value);
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

static nix::DerivedPath derived_path_for_build_input(const nix::StorePath &path) {
    if (path.isDerivation()) {
        return nix::DerivedPath::Built{
            .drvPath = nix::make_ref<const nix::SingleDerivedPath>(
                nix::SingleDerivedPath::Opaque{path}),
            .outputs = nix::OutputsSpec::All{},
        };
    }
    return nix::DerivedPath::Opaque{path};
}

static nix::StorePathSet known_output_paths_from_eval_store(
        nix::Store &s,
        const nix::DerivedPaths &paths,
        const std::shared_ptr<nix::Store> &evalStore) {
    auto &store = evalStore ? *evalStore : s;
    nix::StorePathSet outputPaths;

    for (const auto &path : paths) {
        std::visit(nix::overloaded{
            [&](const nix::DerivedPath::Opaque &) {},
            [&](const nix::DerivedPath::Built &built) {
                auto drvPath = built.drvPath->getBaseStorePath();
                if (!store.isValidPath(drvPath))
                    return;
                for (auto &[outputName, pathOpt] : store.queryStaticPartialDerivationOutputMap(drvPath)) {
                    if (pathOpt)
                        outputPaths.insert(*pathOpt);
                }
            },
        }, path.raw());
    }

    return outputPaths;
}

static bool all_valid_paths(nix::Store &s, const nix::StorePathSet &paths) {
    for (auto &path : paths)
        if (!s.isValidPath(path))
            return false;
    return true;
}

static nb::list successful_build_results_for_targets(
        nix::Store &s,
        const nix::DerivedPaths &paths,
        const std::string &status) {
    nb::list out;
    for (const auto &path : paths) {
        std::visit(nix::overloaded{
            [&](const nix::DerivedPath::Opaque &opaque) {
                out.append(build_result_to_dict(s.printStorePath(opaque.path), true, status, ""));
            },
            [&](const nix::DerivedPath::Built &built) {
                auto drvPath = s.printStorePath(built.drvPath->getBaseStorePath()) + "^*";
                out.append(build_result_to_dict(drvPath, true, status, ""));
            },
        }, path.raw());
    }
    return out;
}

static void copy_target_drvs_from_eval_store(
        nix::Store &s,
        const nix::DerivedPaths &paths,
        const std::shared_ptr<nix::Store> &evalStore) {
    if (!evalStore || evalStore.get() == &s)
        return;

    for (const auto &path : paths) {
        std::visit(nix::overloaded{
            [&](const nix::DerivedPath::Opaque &) {},
            [&](const nix::DerivedPath::Built &built) {
                auto drvPath = built.drvPath->getBaseStorePath();
                if (evalStore->isValidPath(drvPath))
                    nix::copyStorePath(*evalStore, s, drvPath);
            },
        }, path.raw());
    }
}

static void add_store_path_if_present(
        const nix::StoreDirConfig &store,
        nix::StorePathSet &out,
        const std::string &path) {
    try {
        out.insert(store.toStorePath(path).first);
    } catch (nix::Error &) {
        // Not every derivation field is a store path; ignore ordinary strings.
    }
}

static nix::StorePathSet build_input_paths_from_eval_store(
        nix::Store &s,
        const nix::DerivedPaths &paths,
        const std::shared_ptr<nix::Store> &evalStore) {
    auto &store = evalStore ? *evalStore : s;
    nix::StorePathSet inputPaths;

    for (const auto &path : paths) {
        std::visit(nix::overloaded{
            [&](const nix::DerivedPath::Opaque &) {},
            [&](const nix::DerivedPath::Built &built) {
                auto drvPath = built.drvPath->getBaseStorePath();
                if (!store.isValidPath(drvPath))
                    return;
                auto drv = store.readDerivation(drvPath);
                add_store_path_if_present(store, inputPaths, drv.builder);
                for (auto &inputSrc : drv.inputSrcs)
                    inputPaths.insert(inputSrc);
                for (auto &[inputDrv, inputNode] : drv.inputDrvs.map) {
                    if (!store.isValidPath(inputDrv))
                        continue;
                    for (auto &[outputName, pathOpt] : store.queryStaticPartialDerivationOutputMap(inputDrv)) {
                        if (!pathOpt)
                            continue;
                        auto outputs = static_cast<nix::StringSet>(inputNode.value);
                        if (outputs.empty() || outputs.contains(outputName))
                            inputPaths.insert(*pathOpt);
                    }
                }
            },
        }, path.raw());
    }

    return inputPaths;
}

static nb::dict query_missing(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(derived_path_for_build_input(p));
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

// --- GC / roots / maintenance ---

static nix::GcStore &require_gc_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::GcStore *>(&s);
    if (store == nullptr)
        throw nix::Error("store '%s' does not support garbage collection", s.config.getHumanReadableURI());
    return *store;
}

static nix::LocalFSStore &require_local_fs_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::LocalFSStore *>(&s);
    if (store == nullptr)
        throw nix::Error("store '%s' does not support local filesystem roots", s.config.getHumanReadableURI());
    return *store;
}

static nix::IndirectRootStore &require_indirect_root_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::IndirectRootStore *>(&s);
    if (store == nullptr)
        throw nix::Error("store '%s' does not support indirect roots", s.config.getHumanReadableURI());
    return *store;
}

static nix::LogStore &require_log_store(nix::Store &s) {
    auto *store = dynamic_cast<nix::LogStore *>(&s);
    if (store == nullptr)
        throw nix::Error("store '%s' does not support retrieving build logs", s.config.getHumanReadableURI());
    return *store;
}

static nix::ContentAddressMethod request_content_address_method(const nb::dict &request) {
    auto raw = request_string(request, "method");
    if (raw.empty())
        raw = "nar";
    return nix::ContentAddressMethod::parse(raw);
}

static nix::HashAlgorithm request_hash_algo(const nb::dict &request) {
    auto raw = request_string(request, "hash_algo");
    if (raw.empty())
        raw = "sha256";
    return nix::parseHashAlgo(raw);
}

static std::string request_store_add_name(const nb::dict &request) {
    if (auto name = request_optional_string(request, "name"))
        return *name;
    auto path = std::filesystem::path(request_string(request, "path"));
    return path.filename().string();
}

static nix::SourcePath request_source_path(const nb::dict &request) {
    return nix::PosixSourceAccessor::createAtRoot(
        nix::makeParentCanonical(std::filesystem::path(request_string(request, "path"))));
}

static nix::GCAction gc_action_from_int(int action) {
    switch (action) {
        case 1: return nix::GCAction::gcReturnLive;
        case 2: return nix::GCAction::gcReturnDead;
        case 3: return nix::GCAction::gcDeleteDead;
        case 4: return nix::GCAction::gcDeleteSpecific;
        default: return nix::GCAction::gcReturnDead;
    }
}

static nb::list find_roots(nix::Store &s, bool censor) {
    auto roots = require_gc_store(s).findRoots(censor);
    nb::list result;
    for (auto &[target, links] : roots) {
        for (auto &link : links) {
            nb::dict root;
            root["link"] = link;
            root["path"] = store_path_to_dict(target);
            result.append(root);
        }
    }
    return result;
}

static nb::dict collect_garbage(
        nix::Store &s,
        nix::GCAction action,
        bool ignore_liveness,
        const std::vector<nix::StorePath> &paths_to_delete,
        uint64_t max_freed) {
    nix::GCOptions options;
    options.action = action;
    options.ignoreLiveness = ignore_liveness;
    options.pathsToDelete = nix::StorePathSet(paths_to_delete.begin(), paths_to_delete.end());
    options.maxFreed = max_freed;

    nix::GCResults results;
    require_gc_store(s).collectGarbage(options, results);

    nb::dict d;
    d["paths"] = string_set_to_list(results.paths);
    d["bytes_freed"] = nb::int_(results.bytesFreed);
    return d;
}

// --- Build ---

static nb::list build_paths_with_results(
        nix::Store &s,
        const std::vector<nix::StorePath> &paths,
        nix::BuildMode buildMode = nix::bmNormal,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(derived_path_for_build_input(p));
    auto results = s.buildPathsWithResults(dps, buildMode, evalStore);
    nb::list out;
    for (auto &kbr : results) out.append(build_result_from_kbr(kbr, s));
    return out;
}

static nb::list build_for_humans(
        nix::Store &s,
        const std::vector<nix::StorePath> &paths,
        nix::BuildMode buildMode = nix::bmNormal,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(derived_path_for_build_input(p));

    if (!evalStore) {
        try {
            auto missing = s.queryMissing(dps);
            if (!missing.willSubstitute.empty())
                s.substitutePaths(missing.willSubstitute);
        } catch (nix::Error &) {
            // BuildPathsWithResults will report the actionable build failure.
        }
        std::vector<nix::KeyedBuildResult> results;
        try {
            results = s.buildPathsWithResults(dps, buildMode, nullptr);
        } catch (nix::Error &e) {
            e.addTrace({}, "while building paths for build_for_humans");
            throw;
        }
        nb::list out;
        for (auto &kbr : results) out.append(build_result_from_kbr(kbr, s));
        return out;
    }

    nix::StorePathSet knownOutputs;
    try {
        knownOutputs = known_output_paths_from_eval_store(s, dps, evalStore);
    } catch (nix::Error &e) {
        e.addTrace({}, "while discovering build_for_humans output paths");
        throw;
    }
    bool knownOutputsWereValid = !knownOutputs.empty() && all_valid_paths(s, knownOutputs);
    if (!knownOutputs.empty() && !knownOutputsWereValid) {
        try {
            s.substitutePaths(knownOutputs);
        } catch (nix::Error &) {
            // Fall back to an actual build below if substitutes are unavailable.
        }
    }
    if (!knownOutputs.empty() && all_valid_paths(s, knownOutputs))
        return successful_build_results_for_targets(
            s,
            dps,
            knownOutputsWereValid ? "already-valid" : "substituted");

    copy_target_drvs_from_eval_store(s, dps, evalStore);
    auto inputPaths = build_input_paths_from_eval_store(s, dps, evalStore);
    if (!inputPaths.empty())
        s.substitutePaths(inputPaths);
    try {
        auto missing = s.queryMissing(dps);
        if (!missing.willSubstitute.empty())
            s.substitutePaths(missing.willSubstitute);
    } catch (nix::Error &) {
        // Some eval-store derivations reference input .drv paths that are not valid
        // in that eval store. Let buildPathsWithResults handle the eval/build split.
    }
    std::vector<nix::KeyedBuildResult> results;
    try {
        results = s.buildPathsWithResults(dps, buildMode, evalStore);
    } catch (nix::Error &e) {
        e.addTrace({}, "while building paths for build_for_humans");
        throw;
    }
    nb::list out;
    for (auto &kbr : results) out.append(build_result_from_kbr(kbr, s));
    return out;
}

static void build_paths(
        nix::Store &s,
        const std::vector<nix::StorePath> &paths,
        nix::BuildMode buildMode = nix::bmNormal,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(derived_path_for_build_input(p));
    s.buildPaths(dps, buildMode, evalStore);
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
    for (auto &[path, node] : drv.inputDrvs.map) {
        nb::dict entry;
        nb::list outs;
        for (auto &o : node.value) outs.append(o);
        entry["outputs"] = outs;
        nb::dict dynamic_outputs;
        for (auto &[outputName, child] : node.childMap) {
            if (!child.value.empty())
                dynamic_outputs[outputName.c_str()] = *child.value.begin();
        }
        entry["dynamic_outputs"] = dynamic_outputs;
        input_drvs[s.printStorePath(path).c_str()] = entry;
    }
    d["input_drvs"] = input_drvs;

    d["system"] = drv.platform;
    d["builder"] = drv.builder;

    nb::list args;
    for (auto &a : drv.args) args.append(a);
    d["args"] = args;

    nb::dict env;
    for (auto &[k, v] : drv.env) env[k.c_str()] = v;
    d["env"] = env;

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
        nix::Store &s,
        const nb::dict &request,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    auto build_mode = static_cast<nix::BuildMode>(nb::cast<int>(request[nb::str("build_mode")]));
    nb::dict d;
    d["results"] = build_paths_with_results(
        s,
        request_store_paths(s, request, "paths"),
        build_mode,
        evalStore);
    return d;
}

static nb::dict store_build_for_humans(
        nix::Store &s,
        const nb::dict &request,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    auto build_mode = static_cast<nix::BuildMode>(nb::cast<int>(request[nb::str("build_mode")]));
    nb::dict d;
    d["results"] = build_for_humans(
        s,
        request_store_paths(s, request, "paths"),
        build_mode,
        evalStore);
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

static nb::dict store_find_roots(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["roots"] = find_roots(s, request_bool(request, "censor"));
    return d;
}

static nb::dict store_collect_garbage(nix::Store &s, const nb::dict &request) {
    return collect_garbage(
        s,
        gc_action_from_int(nb::cast<int>(request[nb::str("action")])),
        request_bool(request, "ignore_liveness"),
        request_store_paths(s, request, "paths_to_delete"),
        request_uint64(request, "max_freed"));
}

static nb::dict store_add_perm_root(nix::Store &s, const nb::dict &request) {
    auto root = require_local_fs_store(s).addPermRoot(
        request_store_path(s, request, "store_path"),
        request_string(request, "gc_root"));
    nb::dict d;
    d["path"] = root.string();
    return d;
}

static nb::dict store_add_indirect_root(nix::Store &s, const nb::dict &request) {
    require_indirect_root_store(s).addIndirectRoot(request_string(request, "path"));
    return nb::dict();
}

static nb::dict store_ensure_path(nix::Store &s, const nb::dict &request) {
    s.ensurePath(request_store_path(s, request, "path"));
    return nb::dict();
}

static nb::dict store_optimise_store(nix::Store &s, const nb::dict &) {
    s.optimiseStore();
    return nb::dict();
}

static nb::dict store_verify_store(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["errors"] = s.verifyStore(
        request_bool(request, "check_contents"),
        request_bool(request, "repair") ? nix::Repair : nix::NoRepair);
    return d;
}

static nb::dict store_get_build_log(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    auto log = require_log_store(s).getBuildLog(request_store_path(s, request, "path"));
    if (log)
        d["log"] = *log;
    else
        d["log"] = nb::none();
    return d;
}

static nb::dict store_add_to_store(nix::Store &s, const nb::dict &request) {
    return store_path_to_dict(s.addToStoreSlow(
        request_store_add_name(request),
        request_source_path(request),
        request_content_address_method(request),
        request_hash_algo(request),
        {}).path);
}

static nb::dict store_compute_store_path(nix::Store &s, const nb::dict &request) {
    return store_path_to_dict(s.computeStorePath(
        request_store_add_name(request),
        request_source_path(request),
        request_content_address_method(request),
        request_hash_algo(request),
        {}).first);
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
        .def("build_paths", &build_paths, "paths"_a, "build_mode"_a = nix::bmNormal, "eval_store"_a = nullptr)
        .def(
            "build_paths_with_results",
            &build_paths_with_results,
            "paths"_a,
            "build_mode"_a = nix::bmNormal,
            "eval_store"_a = nullptr)
        .def(
            "build_for_humans",
            &build_for_humans,
            "paths"_a,
            "build_mode"_a = nix::bmNormal,
            "eval_store"_a = nullptr)
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
                return require_local_fs_store(s).addPermRoot(store_path, gc_root).string();
            },
            "store_path"_a,
            "gc_root"_a)
        .def(
            "add_indirect_root",
            [](nix::Store &s, const std::string &path) {
                require_indirect_root_store(s).addIndirectRoot(path);
            },
            "path"_a)
        .def("ensure_path", [](nix::Store &s, const nix::StorePath &p) { s.ensurePath(p); }, "path"_a)
        .def("optimise_store", [](nix::Store &s) { s.optimiseStore(); })
        .def(
            "verify_store",
            [](nix::Store &s, bool check_contents, bool repair) {
                return s.verifyStore(check_contents, repair ? nix::Repair : nix::NoRepair);
            },
            "check_contents"_a = false,
            "repair"_a = false)
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
        .def(
            "store_build_paths_with_results",
            &store_build_paths_with_results,
            "request"_a,
            "eval_store"_a = nullptr)
        .def(
            "store_build_for_humans",
            &store_build_for_humans,
            "request"_a,
            "eval_store"_a = nullptr)
        .def("store_read_derivation", &store_read_derivation, "request"_a)
        .def("store_build_derivation", &store_build_derivation, "request"_a)
        .def("store_follow_links_to_store_path", &store_follow_links_to_store_path, "request"_a)
        .def("store_add_temp_root", &store_add_temp_root, "request"_a)
        .def("store_find_roots", &store_find_roots, "request"_a)
        .def("store_collect_garbage", &store_collect_garbage, "request"_a)
        .def("store_add_perm_root", &store_add_perm_root, "request"_a)
        .def("store_add_indirect_root", &store_add_indirect_root, "request"_a)
        .def("store_ensure_path", &store_ensure_path, "request"_a)
        .def("store_optimise_store", &store_optimise_store, "request"_a)
        .def("store_verify_store", &store_verify_store, "request"_a)
        .def("store_get_build_log", &store_get_build_log, "request"_a)
        .def("store_add_to_store", &store_add_to_store, "request"_a)
        .def("store_compute_store_path", &store_compute_store_path, "request"_a);
}

// =========================================================================

NB_MODULE(nanopynix_store, m) {
    m.doc() = "nanopynix: Nix store bindings (StorePath, Store, build)";

    nb::enum_<nix::BuildMode>(m, "BuildMode")
        .value("Normal", nix::BuildMode::bmNormal)
        .value("Repair", nix::BuildMode::bmRepair)
        .value("Check", nix::BuildMode::bmCheck);

    nb::enum_<nix::GCAction>(m, "GCAction")
        .value("ReturnLive", nix::GCAction::gcReturnLive)
        .value("ReturnDead", nix::GCAction::gcReturnDead)
        .value("DeleteDead", nix::GCAction::gcDeleteDead)
        .value("DeleteSpecific", nix::GCAction::gcDeleteSpecific);

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

    // ── Exception bindings ──────────────────────────────────────
    nb::exception<nix::InvalidPath> py_invalid_path(m, "InvalidPath", PyExc_RuntimeError);
    nb::exception<nix::Unsupported> py_unsupported(m, "Unsupported", PyExc_RuntimeError);
    nb::exception<nix::BadStorePath> py_bad_sp(m, "BadStorePath", PyExc_RuntimeError);
    (void) py_invalid_path;
    (void) py_unsupported;
    (void) py_bad_sp;
}
