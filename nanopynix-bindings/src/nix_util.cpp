#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>

#include <nix/store/globals.hh>
#include <nix/util/config-global.hh>
#include <nix/util/configuration.hh>
#include <nix/util/error.hh>
#include <nix/util/experimental-features.hh>
#include <nix/util/hash.hh>
#include <nix/util/logging.hh>
#include <nix/util/url.hh>

#include "nix_error_info.hh"
#include "nix_compat.hh"

#include <memory>
#include <nlohmann/json.hpp>

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// Settings
// =========================================================================

static std::optional<std::string> get_setting(const std::string &name) {
    std::map<std::string, nix::Config::SettingInfo> settings;
    nix::globalConfig.getSettings(settings);
    auto it = settings.find(name);
    if (it != settings.end()) return it->second.value;
    return std::nullopt;
}

static void set_setting(const std::string &name, const std::string &value) {
    if (nix::globalConfig.set(name, value)) return;
    throw std::runtime_error("unknown setting: " + name);
}

static std::map<std::string, std::string> list_settings() {
    std::map<std::string, nix::Config::SettingInfo> settings;
    nix::globalConfig.getSettings(settings);
    std::map<std::string, std::string> out;
    for (auto &[k, v] : settings) out[k] = v.value;
    return out;
}

static std::string list_settings_metadata_json() {
    return nix::globalConfig.toJSON().dump();
}

static std::string current_system() {
    auto evalSystem = get_setting("eval-system");
    if (evalSystem && !evalSystem->empty()) return *evalSystem;
    return nix::settings.thisSystem.get();
}

static void enable_experimental_feature(const std::string &name) {
    auto feature = nix::parseExperimentalFeature(name);
    if (!feature)
        throw std::runtime_error("unknown experimental feature: " + name);
    auto features = nix::experimentalFeatureSettings.experimentalFeatures.get();
    features.insert(*feature);
    nix::experimentalFeatureSettings.experimentalFeatures = features;
}

// =========================================================================

// =========================================================================
// PyLogger — custom Nix logger that forwards to a Python callback
// =========================================================================

// Nix invokes Logger callbacks on the originating Nix thread. A request ID is
// therefore thread-local operation context, not mutable global logger state.
static thread_local int64_t logger_request_id = 0;

class PyLogger : public nix::Logger {
    nb::object _cb;

public:
    explicit PyLogger(nb::object cb) : _cb(std::move(cb)) {}

    ~PyLogger() override {
        nb::gil_scoped_acquire gil;
        _cb = nb::none();
    }

    void log(nix::Verbosity lvl, std::string_view s) override {
        if (lvl > nix::verbosity) return;
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(logger_request_id), "msg", int(lvl), std::string(s));
    }

    void logEI(const nix::ErrorInfo & ei) override {
        if (ei.level > nix::verbosity) return;
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(logger_request_id), "error", int(ei.level), std::string(ei.msg.str()));
    }

    void warn(const std::string & msg) override {
        // Matches nix::Logger::warn's own default impl (log(lvlWarn, ...)),
        // which we fully override rather than delegate to -- so we must
        // apply the same verbosity gate ourselves.
        if (nix::lvlWarn > nix::verbosity) return;
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(logger_request_id), "warn", msg);
    }

    void startActivity(nix::ActivityId id, nix::Verbosity lvl,
                       nix::ActivityType type, const std::string & s,
                       const nix::Logger::Fields & fields,
                       nix::ActivityId parent) override {
        // Matches nix's own default logger (SimpleLogger::startActivity:
        // `if (lvl <= verbosity && !s.empty()) log(...)`) -- most Activities
        // in a big build/copy closure are structural bookkeeping nodes
        // (actRealise/actBuilds/actCopyPaths container activities) with an
        // empty message and no fields, one per derivation/path processed.
        // Even Nix's own CLI renders nothing for these; there is nothing
        // here for any consumer of this callback to use either.
        if (lvl > nix::verbosity || s.empty()) return;
        nb::gil_scoped_acquire gil;
        nb::list fl;
        for (auto & f : fields) {
            if (f.type == nix::Logger::Field::tInt)
                fl.append(nb::int_(f.i));
            else
                fl.append(nb::str(f.s.c_str(), f.s.size()));
        }
        _cb(nb::int_(logger_request_id), "start", id, int(lvl), int(type), s, std::move(fl), parent);
    }

    void stopActivity(nix::ActivityId id) override {
        // Nix constructs one Activity per derivation/store-path/
        // substitution it processes -- hundreds of thousands for a single
        // big deploy's closure, each with a stopActivity call on top of its
        // startActivity. Every consumer of this callback in this
        // repo (pynix/_util.py's _forward_nix_logs, ekn/eval.py's
        // _print_log_event) explicitly discards "stop" events -- nothing
        // downstream ever reads them, at any verbosity. Drop before the
        // GIL-acquiring Python callback and the RPC/protobuf/pydantic round
        // trip they'd otherwise pay for.
        (void) id;
    }

    void result(nix::ActivityId id, nix::ResultType type,
                const nix::Logger::Fields & fields) override {
        // Of all ResultTypes, only resBuildLogLine/resPostBuildLogLine are
        // ever read by a consumer in this repo (both check for exactly
        // those two before doing anything) -- the rest (resProgress/
        // resSetExpected progress-bar ticks foremost among them, firing
        // many times per second per active download/copy) are pure noise
        // here. Drop everything else before the GIL-acquiring Python
        // callback and the RPC/protobuf/pydantic round trip they'd
        // otherwise pay for.
        if (type != nix::resBuildLogLine && type != nix::resPostBuildLogLine) return;
        nb::gil_scoped_acquire gil;
        nb::list fl;
        for (auto & f : fields) {
            if (f.type == nix::Logger::Field::tInt)
                fl.append(nb::int_(f.i));
            else
                fl.append(nb::str(f.s.c_str(), f.s.size()));
        }
        _cb(nb::int_(logger_request_id), "result", id, int(type), std::move(fl));
    }
};

static void install_logger(nb::object cb) {
    nanopynix::nix_compat::install_logger(std::make_unique<PyLogger>(std::move(cb)));
}

static void remove_logger() {
    nanopynix::nix_compat::restore_simple_logger();
}

static void set_logger_request_id(int64_t id) {
    logger_request_id = id;
}

static int64_t get_logger_request_id() {
    return logger_request_id;
}

static void _log_test(const std::string & msg) {
    nanopynix::nix_compat::logger()->log(nix::lvlInfo, msg);
}

static nb::dict build_info() {
    nb::dict capabilities;
    capabilities["logger_unique_ptr"] = NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35;
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
    capabilities["build_result_sum"] = false;
    capabilities["eval_state_mem"] = false;
    capabilities["dynamic_primop_registration"] = false;
    // Whether `nanopynix.StoreImpl.read_derivation` is dispatched at all --
    // `nix::Store::readDerivation` is non-virtual before 2.32, so there is no
    // hook to install. See `py_store_impl.hh`'s dispatch-list comment.
    capabilities["store_impl_read_derivation"] = false;
#else
    capabilities["build_result_sum"] = true;
    capabilities["eval_state_mem"] = true;
    capabilities["dynamic_primop_registration"] = true;
    capabilities["store_impl_read_derivation"] = true;
#endif

    nb::dict info;
    info["nix_version"] = NANOPYNIX_NIX_VERSION;
    info["capabilities"] = std::move(capabilities);
    return info;
}

static void set_verbosity(int lvl) {
    nix::verbosity = (nix::Verbosity) lvl;
}

static int get_verbosity() {
    return (int) nix::verbosity;
}

NB_MODULE(util, m) {
    m.doc() = "nanopynix: Nix util bindings (settings, init)";

    m.def("init_libstore", &nix::initLibStore, nb::call_guard<nb::gil_scoped_release>(),
          "load_config"_a = true,
          "Initialize the Nix store library.");
    m.def("build_info", &build_info,
          "Return the Nix version and compile-time compatibility capabilities for this extension.");

    m.def("set_setting", &set_setting, "name"_a, "value"_a);
    m.def("get_setting", &get_setting, "name"_a);
    m.def("list_settings", &list_settings);
    m.def("list_settings_metadata_json", &list_settings_metadata_json);
    m.def("current_system", &current_system,
          "Return the effective system used by builtins.currentSystem.");
    m.def("enable_experimental_feature", &enable_experimental_feature, "name"_a,
          "Enable an experimental Nix feature (e.g. 'flakes', 'nix-command')");

    m.def("install_logger", &install_logger, "callback"_a,
          "Install a Python callback as the Nix logger.\n"
          "The callback receives (action: str, *args). "
          "Exceptions crash the process.");
    m.def("remove_logger", &remove_logger,
          "Restore the default Nix simple logger.");
    m.def("set_logger_request_id", &set_logger_request_id, "id"_a,
          "Tag subsequent log events with this request ID.");
    m.def("get_logger_request_id", &get_logger_request_id,
          "Get the current logger request ID.");
    m.def("_log_test", &_log_test, "msg"_a,
          "(Internal) Emit a test log message directly via nix::logger->log().");
    m.def("set_verbosity", &set_verbosity, "level"_a,
          "Set Nix log verbosity. 0=Error, 1=Warn, 2=Notice, 3=Info (default), "
          "4=Talkative, 5=Chatty, 6=Debug, 7=Vomit.");
    m.def("get_verbosity", &get_verbosity,
          "Get the current Nix log verbosity level.");

    // ── URL utilities (no init required) ────────────────────────
    m.def("percent_encode", [](const std::string &s, const std::string &keep) -> std::string {
            return nix::percentEncode(s, keep);
          },
          "s"_a, "keep"_a = std::string{},
          "Percent-encode string per RFC 3986. Characters in *keep* are left unencoded.");
    m.def("percent_decode", [](const std::string &s) -> std::string {
            return nix::percentDecode(s);
          },
          "s"_a,
          "Percent-decode string per RFC 3986.");
    m.def("fix_git_url", [](const std::string &url) -> std::string {
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
            auto normalized = nix::fixGitURL(url);
            if (normalized.starts_with("git+"))
                normalized.erase(0, 4);
            return normalized;
#else
            return nix::fixGitURL(url).to_string();
#endif
          },
          "url"_a,
          "Normalize SCP-style and git+https:// URLs to proper URL format.");
    m.def("is_valid_scheme_name", [](const std::string &scheme) -> bool {
            return nix::isValidSchemeName(scheme);
          },
          "scheme"_a,
          "Check whether *scheme* is a valid RFC 3986 scheme name.");

    // ── Hash utilities (no init required) ───────────────────────
    m.def("parse_hash_algo", [](const std::string &s) -> int {
            return static_cast<int>(nix::parseHashAlgo(s));
          },
          "s"_a,
          "Parse a hash algorithm name (e.g. 'sha256') into an int enum.");
    m.def("parse_hash_algo_opt", [](const std::string &s) -> std::optional<int> {
            auto r = nix::parseHashAlgoOpt(s);
            if (r) return static_cast<int>(*r);
            return std::nullopt;
          },
          "s"_a,
          "Parse a hash algorithm name without throwing. Returns None on failure.");
    m.def("print_hash_algo", [](int algo) -> std::string {
            return std::string(nix::printHashAlgo(static_cast<nix::HashAlgorithm>(algo)));
          },
          "algo"_a,
          "Convert a hash algorithm int enum back to its name string.");
    m.def("parse_hash_format", [](const std::string &s) -> int {
            return static_cast<int>(nix::parseHashFormat(s));
          },
          "s"_a,
          "Parse a hash format name (e.g. 'base64', 'nix32', 'sri') into an int enum.");
    m.def("print_hash_format", [](int fmt) -> std::string {
            return std::string(nix::printHashFormat(static_cast<nix::HashFormat>(fmt)));
          },
          "fmt"_a,
          "Convert a hash format int enum back to its name string.");

    m.def("parse_hash_any", [](const std::string &s, std::optional<int> algo_opt) -> std::string {
            std::optional<nix::HashAlgorithm> ha;
            if (algo_opt) ha = static_cast<nix::HashAlgorithm>(*algo_opt);
            auto h = nix::Hash::parseAny(s, ha);
            return h.to_string(nix::HashFormat::SRI, true);
          },
          "s"_a, "algo"_a = nb::none(),
          "Parse a hash string in any format (SRI, hex, base32, base64) and return SRI form.");

    // ── Exception bindings ──────────────────────────────────────
    // Moved to nix_errors.cpp. They used to be spread across this file,
    // nix_expr.cpp and nix_store.cpp, one nanobind translator per type, which
    // made the outcome depend on the order those three modules happened to be
    // imported in -- and forced nix::Error to be left unregistered entirely,
    // since registering the base of every bound type could shadow all of them.
    // One translator owning the whole hierarchy removes both problems.
}
