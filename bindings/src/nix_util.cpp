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

class PyLogger : public nix::Logger {
    nb::object _cb;
    int64_t _req_id = 0;

public:
    explicit PyLogger(nb::object cb) : _cb(std::move(cb)) {}

    ~PyLogger() override {
        nb::gil_scoped_acquire gil;
        _cb = nb::none();
    }

    void set_request_id(int64_t id) { _req_id = id; }
    int64_t request_id() const { return _req_id; }

    void log(nix::Verbosity lvl, std::string_view s) override {
        if (lvl > nix::verbosity) return;
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(_req_id), "msg", int(lvl), std::string(s));
    }

    void logEI(const nix::ErrorInfo & ei) override {
        if (ei.level > nix::verbosity) return;
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(_req_id), "error", int(ei.level), std::string(ei.msg.str()));
    }

    void warn(const std::string & msg) override {
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(_req_id), "warn", msg);
    }

    void startActivity(nix::ActivityId id, nix::Verbosity lvl,
                       nix::ActivityType type, const std::string & s,
                       const nix::Logger::Fields & fields,
                       nix::ActivityId parent) override {
        if (lvl > nix::verbosity) return;
        nb::gil_scoped_acquire gil;
        nb::list fl;
        for (auto & f : fields) {
            if (f.type == nix::Logger::Field::tInt)
                fl.append(nb::int_(f.i));
            else
                fl.append(nb::str(f.s.c_str(), f.s.size()));
        }
        _cb(nb::int_(_req_id), "start", id, int(lvl), int(type), s, std::move(fl), parent);
    }

    void stopActivity(nix::ActivityId id) override {
        nb::gil_scoped_acquire gil;
        _cb(nb::int_(_req_id), "stop", id);
    }

    void result(nix::ActivityId id, nix::ResultType type,
                const nix::Logger::Fields & fields) override {
        nb::gil_scoped_acquire gil;
        nb::list fl;
        for (auto & f : fields) {
            if (f.type == nix::Logger::Field::tInt)
                fl.append(nb::int_(f.i));
            else
                fl.append(nb::str(f.s.c_str(), f.s.size()));
        }
        _cb(nb::int_(_req_id), "result", id, int(type), std::move(fl));
    }
};

static void install_logger(nb::object cb) {
    nix::logger = std::make_unique<PyLogger>(std::move(cb));
}

static void remove_logger() {
    nix::logger = nix::makeSimpleLogger();
}

static void set_logger_request_id(int64_t id) {
    auto *pl = dynamic_cast<PyLogger *>(nix::logger.get());
    if (pl) pl->set_request_id(id);
}

static int64_t get_logger_request_id() {
    auto *pl = dynamic_cast<PyLogger *>(nix::logger.get());
    return pl ? pl->request_id() : 0;
}

static void _log_test(const std::string & msg) {
    nix::logger->log(nix::lvlInfo, msg);
}

static void set_verbosity(int lvl) {
    nix::verbosity = (nix::Verbosity) lvl;
}

static int get_verbosity() {
    return (int) nix::verbosity;
}

NB_MODULE(nanopynix_util, m) {
    m.doc() = "nanopynix: Nix util bindings (settings, init)";

    m.def("init_libstore", &nix::initLibStore, nb::call_guard<nb::gil_scoped_release>(),
          "load_config"_a = true,
          "Initialize the Nix store library.");

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
            return nix::fixGitURL(url).to_string();
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
    // Register specific Nix C++ exceptions as Python types.
    // nb::exception catches strictly by C++ type, so subclasses
    // must be registered BEFORE their base (nix::Error is NOT
    // registered to avoid shadowing more specific translators).
    nb::exception<nix::SysError> py_sys_err(m, "SysError", PyExc_RuntimeError);
    nb::exception<nix::UsageError> py_usage_err(m, "UsageError", PyExc_RuntimeError);
    nb::exception<nix::UnimplementedError> py_unimpl_err(m, "UnimplementedError", PyExc_RuntimeError);
    (void) py_sys_err;
    (void) py_usage_err;
    (void) py_unimpl_err;
}
