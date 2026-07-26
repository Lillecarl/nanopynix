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
#include <nlohmann/json.hpp>
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

#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
#define NANOPYNIX_NIX_HAS_KEYED_BUILD_RESULTS 0
#else
#define NANOPYNIX_NIX_HAS_KEYED_BUILD_RESULTS 1
#endif

#if NANOPYNIX_NIX_HAS_KEYED_BUILD_RESULTS
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
#else
static nb::dict build_result_to_dict(const std::string &drv_path, bool success,
                                      const std::string &status, const std::string &error_msg) {
    nb::dict d;
    d["drv_path"] = drv_path;
    d["success"] = success;
    d["status"] = status;
    d["error_msg"] = error_msg;
    return d;
}

static std::string build_status_str(nix::BuildResult::Status s) {
    using enum nix::BuildResult::Status;
    switch (s) {
        case Built: return "built";
        case Substituted: return "substituted";
        case AlreadyValid: return "already-valid";
        case ResolvesToAlreadyValid: return "resolves-to-already-valid";
        case PermanentFailure: return "permanent-failure";
        case InputRejected: return "input-rejected";
        case OutputRejected: return "output-rejected";
        case TransientFailure: return "transient-failure";
        case CachedFailure: return "cached-failure";
        case TimedOut: return "timed-out";
        case MiscFailure: return "misc-failure";
        case DependencyFailed: return "dependency-failed";
        case LogLimitExceeded: return "log-limit-exceeded";
        case NotDeterministic: return "not-deterministic";
        case NoSubstituters: return "no-substituters";
    }
    return "unknown";
}

static nb::dict build_result_from_kbr(const nix::KeyedBuildResult &kbr,
                                       const nix::StoreDirConfig &store) {
    auto result = static_cast<nix::BuildResult>(kbr);
    return build_result_to_dict(kbr.path.to_string(store), result.success(),
                                build_status_str(result.status), result.errorMsg);
}

static nb::dict build_result_from_br(const nix::BuildResult &br) {
    auto result = br;
    return build_result_to_dict("", result.success(), build_status_str(result.status), result.errorMsg);
}
#endif
#undef NANOPYNIX_NIX_HAS_KEYED_BUILD_RESULTS

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

// ---------------------------------------------------------------------------
// Request-field accessors for the dict-shaped store entrypoints.
//
// These used to index the dict directly. nb::dict's operator[] raises a bare
// Python KeyError naming only the key -- not the operation that rejected it,
// and not one of nanopynix's error types, so it arrives at a caller
// indistinguishable from a bug in their own code. Every one of the 27 dict
// entrypoints behaved that way, including for fields that are optional.
//
// Two rules now:
//   * A field with no default is required. Absent means nix::UsageError naming
//     both the field and the operation -- the same type, and for the same
//     reason, as the copy_closure check further down: a missing argument is a
//     usage error, not a store failure, and UsageError is the registered type
//     that reaches Python as a nanopynix exception.
//   * A field with a default may be omitted. The defaults passed at the call
//     sites below are exactly those the direct API declares in its .def()
//     bindings, so the two APIs cannot drift into disagreeing about what
//     "unset" means.
//
// The proto senders always populate every field, so nothing in the worker path
// changes; this is about a hand-built dict, which is the shape the in-process
// engine would use.
// ---------------------------------------------------------------------------

static nb::object request_field(const nb::dict &request, const char *key, const char *op) {
    if (!request.contains(nb::str(key)))
        throw nix::UsageError("%s: request is missing required field '%s'", op, key);
    return nb::borrow<nb::object>(request[nb::str(key)]);
}

// What a field of each type is called in an error message. Deriving this from
// the C++ type keeps the description at the one place that knows it, instead of
// repeating a string at every call site where it could drift from the cast.
template <typename T> struct request_type_name;
template <> struct request_type_name<std::string> { static constexpr const char *value = "a string"; };
template <> struct request_type_name<bool> { static constexpr const char *value = "a bool"; };
template <> struct request_type_name<uint64_t> { static constexpr const char *value = "an integer"; };
template <> struct request_type_name<int> { static constexpr const char *value = "an integer"; };
template <> struct request_type_name<std::vector<std::string>> {
    static constexpr const char *value = "a list of strings";
};

// A field that is present but of the wrong type is as much a usage error as an
// absent one, and gets the same treatment. nb::cast's own failure is a bare
// std::bad_cast, which reaches Python as RuntimeError('std::bad_cast') -- it
// names neither the field nor the operation, and is not one of nanopynix's
// error types, so a caller catching those misses it entirely.
template <typename T>
static T request_cast(const nb::object &value, const char *key, const char *op) {
    try {
        return nb::cast<T>(value);
    } catch (const nb::cast_error &) {  // NOLINT(bugprone-empty-catch) -- rethrown below with context
    } catch (const nb::python_error &) {  // NOLINT(bugprone-empty-catch) -- ditto; e.g. a failing __index__
    }
    throw nix::UsageError("%s: request field '%s' must be %s, got %s", op, key,
                          request_type_name<T>::value, Py_TYPE(value.ptr())->tp_name);
}

static std::string request_string(const nb::dict &request, const char *key, const char *op) {
    return request_cast<std::string>(request_field(request, key, op), key, op);
}

static std::string request_string(const nb::dict &request, const char *key, const char *op, const char *fallback) {
    if (!request.contains(nb::str(key)))
        return fallback;
    return request_cast<std::string>(request[nb::str(key)], key, op);
}

static bool request_bool(const nb::dict &request, const char *key, const char *op, bool fallback) {
    if (!request.contains(nb::str(key)))
        return fallback;
    return request_cast<bool>(request[nb::str(key)], key, op);
}

static uint64_t request_uint64(const nb::dict &request, const char *key, const char *op, uint64_t fallback) {
    if (!request.contains(nb::str(key)))
        return fallback;
    return request_cast<uint64_t>(request[nb::str(key)], key, op);
}

static int request_int(const nb::dict &request, const char *key, const char *op, int fallback) {
    if (!request.contains(nb::str(key)))
        return fallback;
    return request_cast<int>(request[nb::str(key)], key, op);
}

static std::optional<std::string> request_optional_string(const nb::dict &request, const char *key, const char *op) {
    if (!request.contains(nb::str(key)))
        return std::nullopt;
    auto value = request[nb::str(key)];
    if (value.is_none())
        return std::nullopt;
    return request_cast<std::string>(value, key, op);
}

// Absolutize a caller-supplied path and parse it, rejecting the empty string.
//
// nix::StoreDirConfig::parseStorePath() runs the path through canonPath(),
// which asserts it is non-empty -- and nix wraps __assert_fail, so a violation
// is abort(), not a C++ exception. Nothing downstream can catch it: the whole
// process dies with SIGABRT, taking an in-process caller's session with it.
// Every other malformed path ("x", "/etc/passwd", "garbage") raises
// BadStorePath and arrives in Python as BadStorePathError, so the empty string
// -- trivially reachable from an unset variable or a blank config field -- is
// rejected here with the same error rather than being singled out for a crash.
static nix::StorePath store_path_from_string(nix::Store &s, std::string path, const char *op) {
    if (path.empty())
        throw nix::BadStorePath("%s: store path must not be empty", op);
    if (path[0] != '/') path = s.config.storeDir_ + "/" + path;
    return s.parseStorePath(path);
}

static nix::StorePath request_store_path(nix::Store &s, const nb::dict &request, const char *key, const char *op) {
    return store_path_from_string(s, request_string(request, key, op), op);
}

static std::vector<nix::StorePath> request_store_paths(
        nix::Store &s, const nb::dict &request, const char *key, const char *op, bool required) {
    if (!required && !request.contains(nb::str(key)))
        return {};
    auto raw = request_cast<std::vector<std::string>>(request_field(request, key, op), key, op);
    std::vector<nix::StorePath> paths;
    paths.reserve(raw.size());
    for (auto &path : raw) {
        paths.push_back(store_path_from_string(s, path, op));
    }
    return paths;
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

static nix::DerivedPaths request_derived_paths(
        nix::Store &s, const nb::dict &request, const char *key, const char *op) {
    auto raw = request_cast<std::vector<std::string>>(request_field(request, key, op), key, op);
    nix::DerivedPaths paths;
    paths.reserve(raw.size());
    for (auto &path : raw) {
        if (path.empty())
            throw nix::BadStorePath("%s: store path must not be empty", op);
        if (path[0] != '/') path = s.config.storeDir_ + "/" + path;
        // A plain derivation path has historically meant all of its outputs.
        // A ^ separator opts into Nix's canonical DerivedPath representation.
        if (path.contains('^'))
            paths.push_back(nix::DerivedPath::parse(s.config, path));
        else
            paths.push_back(derived_path_for_build_input(s.parseStorePath(path)));
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

static nb::dict query_missing_store_paths(nix::Store &s, const std::vector<nix::StorePath> &paths) {
    nix::DerivedPaths derived_paths;
    for (auto &path : paths) derived_paths.push_back(derived_path_for_build_input(path));
    return query_missing(s, derived_paths);
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

static nb::list build_paths_with_results(
        nix::Store &s,
        const std::vector<nix::StorePath> &paths,
        nix::BuildMode buildMode = nix::bmNormal,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    nix::DerivedPaths dps;
    for (auto &p : paths) dps.push_back(derived_path_for_build_input(p));
    std::vector<nix::KeyedBuildResult> results;
    {
        nb::gil_scoped_release release;
        results = s.buildPathsWithResults(dps, buildMode, evalStore);
    }
    nb::list out;
    for (auto &kbr : results) out.append(build_result_from_kbr(kbr, s));
    return out;
}

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
    nb::gil_scoped_release release;
    s.buildPaths(dps, buildMode, evalStore);
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
    nix::BuildResult result;
    {
        nb::gil_scoped_release release;
        auto drv = s.readDerivation(drvPath);
        result = s.buildDerivation(
            drvPath, static_cast<const nix::BasicDerivation &>(drv), buildMode);
    }
    return build_result_from_br(result);
}

static std::string store_uri(nix::Store &s, bool with_params) {
    return with_params ? s.config.getReference().render() : s.config.getHumanReadableURI();
}

static nb::dict store_get_uri(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["uri"] = store_uri(s, request_bool(request, "with_params", "store_get_uri", false));
    return d;
}

static nb::dict store_get_store_dir(nix::Store &s, const nb::dict &) {
    nb::dict d;
    d["dir"] = std::string(s.config.storeDir_);
    return d;
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

static nb::dict store_get_store_dirs(nix::Store &s, const nb::dict &) {
    return store_dirs_to_dict(s);
}

static nb::dict store_get_store_dirs_direct(nix::Store &s) {
    return store_dirs_to_dict(s);
}

static nb::dict store_is_valid_path(nix::Store &s, const nb::dict &request) {
    auto path = request_store_path(s, request, "path", "store_is_valid_path");
    bool valid;
    {
        nb::gil_scoped_release release;
        valid = s.isValidPath(path);
    }
    nb::dict d;
    d["valid"] = valid;
    return d;
}

static nb::dict store_parse_store_path(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["path"] = store_path_to_string(s, request_store_path(s, request, "path", "store_parse_store_path"));
    return d;
}

static nb::dict store_query_path_info(nix::Store &s, const nb::dict &request) {
    return query_path_info(s, request_store_path(s, request, "path", "store_query_path_info"));
}

static nb::dict store_query_path_from_hash_part(
        nix::Store &s, const nb::dict &request) {
    auto hash_part = request_string(request, "hash_part", "store_query_path_from_hash_part");
    std::optional<nix::StorePath> path;
    {
        nb::gil_scoped_release release;
        path = s.queryPathFromHashPart(hash_part);
    }
    nb::dict d;
    if (path) d["path"] = store_path_to_string(s, *path);
    else d["path"] = nb::none();
    return d;
}

static nb::dict store_compute_fs_closure(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = compute_fs_closure(
        s,
        request_store_path(s, request, "path", "store_compute_fs_closure"),
        request_bool(request, "flip_direction", "store_compute_fs_closure", false),
        request_bool(request, "include_outputs", "store_compute_fs_closure", false),
        request_bool(request, "include_derivers", "store_compute_fs_closure", false));
    return d;
}

static nb::dict store_query_missing(nix::Store &s, const nb::dict &request) {
    return query_missing(s, request_derived_paths(s, request, "derived_paths", "store_query_missing"));
}

static nb::dict store_query_derivation_outputs(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_derivation_outputs(s, request_store_path(s, request, "path", "store_query_derivation_outputs"));
    return d;
}

static nb::dict store_query_valid_derivers(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_valid_derivers(s, request_store_path(s, request, "path", "store_query_valid_derivers"));
    return d;
}

static nb::dict store_query_all_valid_paths(nix::Store &s, const nb::dict &) {
    nb::dict d;
    d["paths"] = query_all_valid_paths(s);
    return d;
}

static nb::dict store_query_referrers(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_referrers(s, request_store_path(s, request, "path", "store_query_referrers"));
    return d;
}

static nb::dict store_query_substitutable_paths(
        nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["paths"] = query_substitutable_paths(s, request_store_paths(s, request, "paths", "store_query_substitutable_paths", true));
    return d;
}

static nb::dict store_build_paths_with_results(
        nix::Store &s,
        const nb::dict &request,
        std::shared_ptr<nix::Store> evalStore = nullptr) {
    auto build_mode = static_cast<nix::BuildMode>(request_int(request, "build_mode", "store_build_paths_with_results", nix::bmNormal));
    nb::dict d;
    d["results"] = build_derived_paths_with_results(
        s,
        request_derived_paths(s, request, "derived_paths", "store_build_paths_with_results"),
        build_mode,
        evalStore);
    return d;
}

static nb::dict store_read_derivation(nix::Store &s, const nb::dict &request) {
    return read_derivation(s, request_store_path(s, request, "path", "store_read_derivation"));
}

static nb::dict store_build_derivation(nix::Store &s, const nb::dict &request) {
    return build_derivation(
        s,
        request_store_path(s, request, "path", "store_build_derivation"),
        static_cast<nix::BuildMode>(request_int(request, "build_mode", "store_build_derivation", nix::bmNormal)));
}

static nb::dict store_follow_links_to_store_path(
        nix::Store &s, const nb::dict &request) {
    auto input_path = request_string(request, "path", "store_follow_links_to_store_path");
    std::optional<nix::StorePath> path;
    {
        nb::gil_scoped_release release;
        path.emplace(s.followLinksToStorePath(input_path));
    }
    nb::dict d;
    d["path"] = store_path_to_string(s, *path);
    return d;
}

static nb::dict store_add_temp_root(nix::Store &s, const nb::dict &request) {
    auto path = request_store_path(s, request, "path", "store_add_temp_root");
    {
        nb::gil_scoped_release release;
        s.addTempRoot(path);
    }
    return nb::dict();
}

static nb::dict store_find_roots(nix::Store &s, const nb::dict &request) {
    nb::dict d;
    d["roots"] = find_roots(s, request_bool(request, "censor", "store_find_roots", true));
    return d;
}

static nb::dict store_collect_garbage(nix::Store &s, const nb::dict &request) {
    return collect_garbage(
        s,
        gc_action_from_int(request_cast<int>(
            request_field(request, "action", "store_collect_garbage"), "action", "store_collect_garbage")),
        request_bool(request, "ignore_liveness", "store_collect_garbage", false),
        request_store_paths(s, request, "paths_to_delete", "store_collect_garbage", false),
        request_uint64(request, "max_freed", "store_collect_garbage", std::numeric_limits<uint64_t>::max()));
}

static nb::dict store_add_perm_root(nix::Store &s, const nb::dict &request) {
    auto store_path = request_store_path(s, request, "store_path", "store_add_perm_root");
    auto gc_root = request_string(request, "gc_root", "store_add_perm_root");
    std::filesystem::path root;
    {
        nb::gil_scoped_release release;
        root = require_local_fs_store(s).addPermRoot(store_path, gc_root);
    }
    nb::dict d;
    d["path"] = root.string();
    return d;
}

static nb::dict store_add_indirect_root(nix::Store &s, const nb::dict &request) {
    auto path = request_string(request, "path", "store_add_indirect_root");
    {
        nb::gil_scoped_release release;
        require_indirect_root_store(s).addIndirectRoot(path);
    }
    return nb::dict();
}

static nb::dict store_ensure_path(nix::Store &s, const nb::dict &request) {
    auto path = request_store_path(s, request, "path", "store_ensure_path");
    {
        nb::gil_scoped_release release;
        s.ensurePath(path);
    }
    return nb::dict();
}

static nb::dict store_copy_closure(
        nix::Store &s,
        const nb::dict &request,
        std::shared_ptr<nix::Store> destStore = nullptr) {
    if (!destStore) {
        // A missing argument, not a store failure -- nix::UsageError is the
        // registered type for that, and unlike nix::Error it reaches Python
        // inside the NixError hierarchy.
        throw nix::UsageError("dest_store_handle is required for copy_closure");
    }
    copy_closure(
        s,
        request_store_paths(s, request, "paths", "store_copy_closure", true),
        destStore,
        request_bool(request, "repair", "store_copy_closure", false),
        request_bool(request, "check_sigs", "store_copy_closure", true),
        request_bool(request, "substitute", "store_copy_closure", false));
    return nb::dict();
}

static nb::dict store_optimise_store(nix::Store &s, const nb::dict &) {
    {
        nb::gil_scoped_release release;
        s.optimiseStore();
    }
    return nb::dict();
}

static nb::dict store_verify_store(nix::Store &s, const nb::dict &request) {
    auto check_contents = request_bool(request, "check_contents", "store_verify_store", false);
    auto repair = request_bool(request, "repair", "store_verify_store", false);
    size_t errors;
    {
        nb::gil_scoped_release release;
        errors = s.verifyStore(check_contents, repair ? nix::Repair : nix::NoRepair);
    }
    nb::dict d;
    d["errors"] = errors;
    return d;
}

static std::optional<std::string> get_build_log(nix::Store &s, const nix::StorePath &path) {
    {
        nb::gil_scoped_release release;
        return require_log_store(s).getBuildLog(path);
    }
}

static nb::dict store_get_build_log(nix::Store &s, const nb::dict &request) {
    auto log = get_build_log(s, request_store_path(s, request, "path", "store_get_build_log"));
    nb::dict d;
    if (log)
        d["log"] = *log;
    else
        d["log"] = nb::none();
    return d;
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
static nb::dict store_add_to_store(nix::Store &s, const nb::dict &request) {
    auto path = add_to_store(
        s,
        request_string(request, "path", "store_add_to_store"),
        request_optional_string(request, "name", "store_add_to_store"),
        request_string(request, "method", "store_add_to_store", "nar"),
        request_string(request, "hash_algo", "store_add_to_store", "sha256"));
    nb::dict d;
    d["path"] = store_path_to_string(s, path);
    return d;
}

static nb::dict store_compute_store_path(nix::Store &s, const nb::dict &request) {
    auto path = compute_store_path(
        s,
        request_string(request, "path", "store_compute_store_path"),
        request_optional_string(request, "name", "store_compute_store_path"),
        request_string(request, "method", "store_compute_store_path", "nar"),
        request_string(request, "hash_algo", "store_compute_store_path", "sha256"));
    nb::dict d;
    d["path"] = store_path_to_string(s, path);
    return d;
}

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
        // Unlike request_store_path()'s funnel this deliberately does not
        // absolutize a relative path -- the direct API has always required an
        // absolute one -- but it needs the same empty-string guard, since
        // parseStorePath() aborts the process rather than throwing on it.
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
        // Build
        .def("build_paths", &build_paths, "paths"_a, "build_mode"_a = nix::bmNormal, "eval_store"_a = nullptr)
        .def(
            "build_paths_with_results",
            &build_paths_with_results,
            "paths"_a,
            "build_mode"_a = nix::bmNormal,
            "eval_store"_a = nullptr)
        .def("read_derivation", &read_derivation, "drv_path"_a)
        .def("build_derivation", &build_derivation, "drv_path"_a, "build_mode"_a)
        // Path info
        .def("query_path_info", &query_path_info, "path"_a)
        .def("query_path_from_hash_part",
             [](nix::Store &s, const std::string &h) { return s.queryPathFromHashPart(h); },
             nb::call_guard<nb::gil_scoped_release>(), "hash"_a)
        // Closures
        .def("compute_fs_closure", &compute_fs_closure,
             "path"_a, "flip_direction"_a = false, "include_outputs"_a = false, "include_derivers"_a = false)
        .def("copy_closure", &copy_closure,
             "paths"_a, "dest_store"_a, "repair"_a = false, "check_sigs"_a = true, "substitute"_a = false)
        .def("query_missing", &query_missing_store_paths, "paths"_a)
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
        .def("store_get_uri", &store_get_uri, "request"_a)
        .def("store_get_store_dir", &store_get_store_dir, "request"_a)
        .def("store_get_store_dirs", &store_get_store_dirs, "request"_a)
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
        .def("store_read_derivation", &store_read_derivation, "request"_a)
        .def("store_build_derivation", &store_build_derivation, "request"_a)
        .def("store_follow_links_to_store_path", &store_follow_links_to_store_path, "request"_a)
        .def("store_add_temp_root", &store_add_temp_root, "request"_a)
        .def("store_find_roots", &store_find_roots, "request"_a)
        .def("store_collect_garbage", &store_collect_garbage, "request"_a)
        .def("store_add_perm_root", &store_add_perm_root, "request"_a)
        .def("store_add_indirect_root", &store_add_indirect_root, "request"_a)
        .def("store_ensure_path", &store_ensure_path, "request"_a)
        .def(
            "store_copy_closure",
            &store_copy_closure,
            "request"_a,
            "dest_store"_a = nullptr)
        .def("store_optimise_store", &store_optimise_store, "request"_a)
        .def("store_verify_store", &store_verify_store, "request"_a)
        .def("store_get_build_log", &store_get_build_log, "request"_a)
        .def("store_add_to_store", &store_add_to_store, "request"_a)
        .def("store_compute_store_path", &store_compute_store_path, "request"_a);
}

// =========================================================================

NB_MODULE(store, m) {
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
