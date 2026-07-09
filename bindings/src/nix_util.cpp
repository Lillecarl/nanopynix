#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>

#include <nix/store/globals.hh>
#include <nix/util/configuration.hh>
#include <nix/util/error.hh>
#include <nix/util/experimental-features.hh>
#include <nix/util/logging.hh>

#include <memory>

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// Settings
// =========================================================================

static std::optional<std::string> get_setting(const std::string &name) {
    // Try main settings first
    {
        std::map<std::string, nix::Config::SettingInfo> settings;
        nix::settings.getSettings(settings);
        auto it = settings.find(name);
        if (it != settings.end()) return it->second.value;
    }
    // Fall back to experimental feature settings
    {
        std::map<std::string, nix::Config::SettingInfo> settings;
        nix::experimentalFeatureSettings.getSettings(settings);
        auto it = settings.find(name);
        if (it != settings.end()) return it->second.value;
    }
    return std::nullopt;
}

static void set_setting(const std::string &name, const std::string &value) {
    if (nix::settings.set(name, value)) return;
    if (nix::experimentalFeatureSettings.set(name, value)) return;
    throw std::runtime_error("unknown setting: " + name);
}

static std::map<std::string, std::string> list_settings() {
    std::map<std::string, nix::Config::SettingInfo> settings;
    nix::settings.getSettings(settings);
    nix::experimentalFeatureSettings.getSettings(settings);
    std::map<std::string, std::string> out;
    for (auto &[k, v] : settings) out[k] = v.value;
    return out;
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

    m.def("init_libstore", &nix::initLibStore,
          "load_config"_a = true,
          "Initialize the Nix store library.");

    m.def("set_setting", &set_setting, "name"_a, "value"_a);
    m.def("get_setting", &get_setting, "name"_a);
    m.def("list_settings", &list_settings);
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
