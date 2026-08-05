#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>

// For `NIX_USE_BOEHMGC`, which `build_info` publishes as a capability. The
// only libexpr header this file needs, and it holds macros alone.
#include <nix/expr/config.hh>

#include <nix/store/globals.hh>
#include <nix/util/config-global.hh>
#include <nix/util/configuration.hh>
#include <nix/util/error.hh>
#include <nix/util/experimental-features.hh>
#include <nix/util/hash.hh>
#include <nix/util/logging.hh>
#include <nix/util/terminal.hh>
#include <nix/util/url.hh>

#include "nix_error_info.hh"
#include "nix_compat.hh"

#include <atomic>
#include <cctype>
#include <cstdlib>
#include <limits>
#include <map>
#include <memory>
#include <string>
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

static std::map<std::string, std::string> list_settings(bool overridden_only) {
    std::map<std::string, nix::Config::SettingInfo> settings;
    nix::globalConfig.getSettings(settings, overridden_only);
    std::map<std::string, std::string> out;
    for (auto &[k, v] : settings) out[k] = v.value;
    return out;
}

static void reset_overridden() {
    nix::globalConfig.resetOverridden();
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

// The same argument applies to the verbosity, and here it is a correctness
// argument rather than a design preference.
//
// `nix::verbosity` (logging.hh) is a plain non-atomic global. Every `debug()`
// and `printInfo()` call site reads it on its own thread, through the
// `printMsg` macro, while a caller that changes the level writes it from
// whichever Nix thread serves that call. ThreadSanitizer reports the race,
// and the race is real: the type gives no atomicity, and patching a public
// Nix header that hundreds of call sites read is not an option.
//
// So the global stops being a variable. `nanopynix_bind_util` writes it once,
// at import, and pins it wide open. The filter moves here, to a thread-local
// that only its own thread reads and writes. A caller therefore gets a level
// per operation, and no thread can observe another thread's write.
//
// The price is that Nix's own `printMsg` gate stops rejecting a message
// before it costs anything, so every call site at or under the ceiling
// formats its message before `PyLogger` drops it.
//
// **The ceiling is `chatty`, and that is a measured choice.** Two workloads
// disagree about the price, and the second one is why the ceiling is not
// `vomit`.
//
// Evaluation and store work pay nothing. Five rounds for each cell, median
// wall clock, and every run repeated to show the noise:
//
//   through the daemon, and the evaluator in this process
//   ceiling  eval fold   2000 query_path_info   200-root closure walk
//   vomit    0.043 s     0.542 s                0.919 s
//   chatty   0.044 s     0.560 s                0.911 s
//   error    0.043 s     0.565 s                0.938 s
//
//   LocalStore in this process, which is where the 139 libstore `debug()`
//   sites are, so it is the cell that had to be filled separately
//   ceiling  300 query_path_info   300 is_valid_path
//   vomit    0.078 / 0.073 s       0.069 / 0.067 s
//   chatty   0.070 / 0.085 s       0.061 / 0.067 s
//   error    0.089 / 0.078 s       0.081 / 0.071 s
//
// Flake work does not. `pynix flake show` of a git flake, on Nix 2.31,
// through the rpc engine with a cold fetch cache and the 30 second deadline
// that CI gives an rpc call:
//
//   ceiling  outcome of the five tests
//   error    5 passed
//   chatty   5 passed
//   debug    5 failed, each on DEADLINE_EXCEEDED in `eval_flake`
//   vomit    5 failed, the same way
//
// A flake evaluation fetches, and the fetch path logs far more than a store
// query does. `debug` fails exactly as `vomit` does, because `printMsg`
// compares `lvl <= nix::verbosity`: a ceiling of `debug` already admits all
// 208 `debug` sites, and the 5 `vomit` sites above them add nothing. So the
// cliff is between `chatty` and `debug`, and `chatty` is the highest ceiling
// that costs nothing.
//
// Two facts explain why the levels at or under `chatty` are free. The
// high-volume path never had a gate: `Activity::Activity`
// (libutil/logging.cc) calls `startActivity` unconditionally, and the free
// function `warn()` calls `logger->warn` unconditionally. And libexpr, which
// is the evaluation hot loop, holds 19 `printMsg` sites in the whole of Nix
// 2.34, against 208 `debug` sites across the tree.
//
// **What the ceiling costs a caller.** `set_verbosity(DEBUG)` is accepted and
// changes what this thread would print, but Nix drops a `debug` message
// before the filter ever sees it, so no `debug` message arrives. Read the
// ceiling with `get_log_ceiling` to learn this, and raise it with
// `NANOPYNIX_LOG_CEILING` at import when a workload wants `debug` more than
// it wants the fetch path to be fast.

// The level a thread starts at. Nix starts threads of its own -- a
// substituter, a build hook reader -- and those never pass through the
// dispatch wrapper that sets the thread-local below, so they need a default.
// An atomic, because `set_default_verbosity` publishes to it from a Nix
// thread while another thread's thread-local initializer reads it.
static std::atomic<int> default_verbosity{(int) nix::lvlInfo};

// The level this thread filters at. The initializer runs once per thread, on
// the thread's first log call or first `set_verbosity`.
static thread_local nix::Verbosity thread_verbosity =
    (nix::Verbosity) default_verbosity.load(std::memory_order_relaxed);

// **One `Logger` for the life of the process, and it is never freed.**
//
// This class used to be installed and destroyed for each session:
// `install_logger` put a new one in `nix::logger`, and `remove_logger` put
// Nix's simple logger back, which freed it. ThreadSanitizer showed why that
// cannot be right (issue #66):
//
//   Write of size 8 ... operator delete(void*, unsigned long)   [_ext]
//   Previous read of size 8 ... nix::Activity::~Activity()      [curl worker]
//       nix::curlFileTransfer::TransferItem::~TransferItem()
//       nix::curlFileTransfer::workerThreadMain()
//
// `Activity` holds a `Logger &` and calls `logger.stopActivity(id)` when it
// dies. Nix starts the curl file-transfer thread inside `curlFileTransfer`'s
// own constructor, which `getDefaultSubstituters` reaches, and **it gives no
// way to join that thread**. So the thread outlives the session, and a free
// of the logger is a free of an object another thread is still reading.
//
// Two properties make this safe, and both are structural rather than a
// tightened window:
//
// 1. `nix::logger` is written exactly once, by `nanopynix_bind_util` at module
//    import, on the main thread, before any Nix thread exists. Every later
//    read has a happens-before edge to that write.
// 2. This object is a leaked singleton. `install_logger` and `remove_logger`
//    only swap the Python callback inside it. There is no `operator delete`
//    left for a Nix thread to race with.
//
// The callback itself needs no separate lock: every method that reads `_cb`
// holds the GIL, and both swap functions take the GIL to write it. `_active`
// is the fast path only -- a caller that reads it as true and then loses the
// race finds `_cb` already `None` under the GIL, and drops the message the
// same way.
class PyLogger : public nix::Logger {
    // GIL-guarded. `None` means no session has installed a callback, and the
    // fallback below is what gets the message.
    nb::object _cb;
    // The gate before the GIL. Not the authority -- `_cb` is.
    std::atomic<bool> _active{false};
    // Nix's own logger, kept for the life of the process. It is what
    // `remove_logger` restores, and owning it here is what lets that
    // restoration leave `nix::logger` alone.
    std::unique_ptr<nix::Logger> _fallback{nix::makeSimpleLogger()};

public:
    PyLogger() = default;

    // The one free that is left, and it is not a session's to cause: libutil
    // constructs `nix::logger` at static-init time, so this runs at process
    // exit and nowhere else. It drops the reference to the callback rather
    // than releasing it, because the interpreter may already be finalized and
    // a `Py_DECREF` there is undefined.
    ~PyLogger() override { (void) _cb.release(); }

    /// Start forwarding to `cb`. A second call replaces the first.
    void attach(nb::object cb) {
        // The caller already holds the GIL: it came from Python.
        _cb = std::move(cb);
        _active.store(true, std::memory_order_release);
    }

    /// Stop forwarding, and give the messages back to Nix's own logger.
    void detach() {
        _active.store(false, std::memory_order_release);
        _cb = nb::none();
    }

    bool attached() const { return _active.load(std::memory_order_acquire); }

    void log(nix::Verbosity lvl, std::string_view s) override {
        if (lvl > thread_verbosity) return;
        if (!attached()) { _fallback->log(lvl, s); return; }
        nb::gil_scoped_acquire gil;
        if (_cb.is_none()) return;
        _cb(nb::int_(logger_request_id), "msg", int(lvl), std::string(s));
    }

    // The structured payload rides beside the flat message, and it is the
    // dict `errinfo::to_dict` already builds for `NixError.info`. One builder
    // serves both, so the log path stops carrying less than Nix's own
    // `JSONLogger::logEI`, which emits every one of these fields.
    //
    // `to_dict` reads no source. It renders each position with `Pos::print`,
    // which prints the origin; `Pos::getCodeLines` is what would read a file,
    // and only `showErrorInfo(showTrace=true)` calls that. So this costs a
    // dict per error event and no I/O. Issue #48.
    void logEI(const nix::ErrorInfo & ei) override {
        if (ei.level > thread_verbosity) return;
        if (!attached()) { _fallback->logEI(ei); return; }
        nb::gil_scoped_acquire gil;
        if (_cb.is_none()) return;
        _cb(nb::int_(logger_request_id), "error", int(ei.level), std::string(ei.msg.str()),
            nanopynix::errinfo::to_dict(ei));
    }

    void warn(const std::string & msg) override {
        // Matches nix::Logger::warn's own default impl (log(lvlWarn, ...)),
        // which we fully override rather than delegate to -- so we must
        // apply the same verbosity gate ourselves.
        if (nix::lvlWarn > thread_verbosity) return;
        if (!attached()) { _fallback->warn(msg); return; }
        nb::gil_scoped_acquire gil;
        if (_cb.is_none()) return;
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
        if (lvl > thread_verbosity || s.empty()) return;
        if (!attached()) { _fallback->startActivity(id, lvl, type, s, fields, parent); return; }
        nb::gil_scoped_acquire gil;
        if (_cb.is_none()) return;
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
        //
        // No fallback delegation either: `nix::Logger::stopActivity` is an
        // empty default and `SimpleLogger` does not override it, so a
        // delegated call would do exactly this.
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
        //
        // `SimpleLogger::result` does read `resBuildLogLine`, under
        // `printBuildLogs`, so the detached path delegates rather than drops.
        if (!attached()) { _fallback->result(id, type, fields); return; }
        if (type != nix::resBuildLogLine && type != nix::resPostBuildLogLine) return;
        nb::gil_scoped_acquire gil;
        if (_cb.is_none()) return;
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

// The one `PyLogger` of the process. Leaked on purpose: see the class comment.
// `nanopynix_bind_util` puts it in `nix::logger` at import, and nothing ever
// takes it out again.
static PyLogger * py_logger = nullptr;

static PyLogger & require_py_logger() {
    if (py_logger == nullptr)
        throw std::runtime_error("the nanopynix logger is not installed; the extension module did not finish importing");
    return *py_logger;
}

static void install_logger(nb::object cb) {
    require_py_logger().attach(std::move(cb));
}

static void remove_logger() {
    require_py_logger().detach();
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

    // Whether libexpr in this build has the Boehm collector.
    //
    // The one capability here that a Nix version does not decide. It comes
    // from `-Dgc=disabled`, which nanopynix builds on purpose for the
    // AddressSanitizer variant: libexpr refuses ASAN together with a
    // conservative collector. Without the collector the evaluator allocates
    // and never releases, and the process is the unit of reclamation.
    //
    // `_gc_collect` and `_gc_stats` raise in such a build, so a caller that
    // measures the Nix heap asks this first.
    capabilities["boehm_gc"] = static_cast<bool>(NIX_USE_BOEHMGC);

    nb::dict info;
    info["nix_version"] = NANOPYNIX_NIX_VERSION;
    info["capabilities"] = std::move(capabilities);
    return info;
}

static void set_verbosity(int lvl) {
    thread_verbosity = (nix::Verbosity) lvl;
}

static int get_verbosity() {
    return (int) thread_verbosity;
}

// The level a Nix thread starts at. A caller reads this to learn Nix's own
// compiled-in level, which is what an unconfigured session reports.
static int get_default_verbosity() {
    return default_verbosity.load(std::memory_order_relaxed);
}

// Deliberately separate from `set_verbosity`. A caller that runs one
// operation at one level saves the thread level, sets it, and restores it,
// and that restore must not undo a session-wide change that happened while
// the operation ran. So the two levels move independently.
static void set_default_verbosity(int lvl) {
    default_verbosity.store(lvl, std::memory_order_relaxed);
}

// The pinned level that Nix's own `printMsg` gate compares against. A caller
// that asks why a workload is slow, or why a message it expected never
// reached the filter, reads this.
static int get_log_ceiling() {
    return (int) nix::verbosity;
}

// The default ceiling. `chatty` is the highest level that costs nothing --
// read the measurement at the top of this file, which shows `debug` losing
// the flake tests to a deadline and `chatty` keeping them.
static const nix::Verbosity default_log_ceiling = nix::lvlChatty;

// Read `NANOPYNIX_LOG_CEILING` once, at import. A name, as `LogLevel` accepts
// ("debug"), or a number. An unusable value keeps the default rather than
// failing the import: a mistyped environment variable must not stop the
// library from loading, and `get_log_ceiling()` reports what took effect.
static int parse_log_ceiling() {
    const char *raw = std::getenv("NANOPYNIX_LOG_CEILING");
    if (raw == nullptr || *raw == '\0') return (int) default_log_ceiling;

    static const std::map<std::string, nix::Verbosity> names = {
        {"error", nix::lvlError},         {"warn", nix::lvlWarn},
        {"notice", nix::lvlNotice},       {"info", nix::lvlInfo},
        {"talkative", nix::lvlTalkative}, {"chatty", nix::lvlChatty},
        {"debug", nix::lvlDebug},         {"vomit", nix::lvlVomit},
    };
    std::string key(raw);
    for (auto &c : key) c = (char) std::tolower((unsigned char) c);
    auto it = names.find(key);
    if (it != names.end()) return (int) it->second;

    try {
        int lvl = std::stoi(key);
        if (lvl >= (int) nix::lvlError && lvl <= (int) nix::lvlVomit) return lvl;
    } catch (const std::exception &) {
        // Not a number either. Fall through to the default below.
    }
    return (int) default_log_ceiling;
}

void nanopynix_bind_util(nb::module_ &m) {
    m.doc() = "nanopynix: Nix util bindings (settings, init)";

    // Pin the global, here and nowhere else. This runs on the import thread,
    // before the process holds an executor or any other Nix thread, so thread
    // creation gives every later read a happens-before edge to this write.
    // That property is what removes the race, and a second write anywhere
    // would give it back. Read `thread_verbosity` above for the whole
    // argument.
    //
    // Capture Nix's compiled-in level first: it is what an unconfigured
    // session reports, and hardcoding a constant here would make that a lie
    // the day Nix changes it.
    default_verbosity.store((int) nix::verbosity, std::memory_order_relaxed);
    nix::verbosity = (nix::Verbosity) parse_log_ceiling();

    // The same argument, and the same one write. `nix::logger` is a plain
    // global that a Nix thread reads on every log line, and `nix::Activity`
    // keeps a `Logger &` past the life of the session that made it. So the
    // pointer is written here, once, before any Nix thread exists, and the
    // object it points at is never freed. `install_logger` and
    // `remove_logger` swap only the callback inside it. Issue #66 gives the
    // ThreadSanitizer report that this shape answers.
    py_logger = new PyLogger();
    nanopynix::nix_compat::install_logger(std::unique_ptr<nix::Logger>(py_logger));

    m.def("init_libstore", &nix::initLibStore, nb::call_guard<nb::gil_scoped_release>(),
          "load_config"_a = true,
          "Initialize the Nix store library.");
    m.def("build_info", &build_info,
          "Return the Nix version and compile-time compatibility capabilities for this extension.");

    m.def("set_setting", &set_setting, "name"_a, "value"_a);
    m.def("get_setting", &get_setting, "name"_a);
    m.def("list_settings", &list_settings, "overridden_only"_a = false,
          "Return the effective value of every registered global setting.\n\n"
          "With overridden_only=True, return only the settings something has "
          "actually set, rather than every setting with its default. Nix tracks "
          "this per setting (`AbstractSetting::overridden`), and combined with "
          "reset_overridden() it tells apart what a nix.conf supplied from what "
          "the caller changed afterwards.");
    m.def("reset_overridden", &reset_overridden,
          "Clear the 'overridden' flag on every registered global setting.\n\n"
          "Does NOT change any value. It only resets the bookkeeping, so a "
          "later list_settings(overridden_only=True) reports what was set after "
          "this call rather than everything set before it.");
    m.def("list_settings_metadata_json", &list_settings_metadata_json);
    m.def("current_system", &current_system,
          "Return the effective system used by builtins.currentSystem.");
    m.def("enable_experimental_feature", &enable_experimental_feature, "name"_a,
          "Enable an experimental Nix feature (e.g. 'flakes', 'nix-command')");

    m.def("install_logger", &install_logger, "callback"_a,
          "Install a Python callback as the Nix logger.\n"
          // Double backticks make this an RST literal. Without them the `*` of
          // `*args` opens inline emphasis, and the docs build fails on the
          // warning because sphinx-build runs with -W.
          "The callback receives ``(action: str, *args)``. "
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
          "Set the Nix log verbosity of the calling thread.\n\n"
          "0=Error, 1=Warn, 2=Notice, 3=Info (default), 4=Talkative, 5=Chatty, "
          "6=Debug, 7=Vomit.\n\n"
          "The level is thread-local, because Nix invokes the logger on the "
          "thread that produced the message. A caller that wants one level for "
          "one operation sets it on the thread that runs that operation, and "
          "restores it afterwards.");
    m.def("get_verbosity", &get_verbosity,
          "Get the Nix log verbosity of the calling thread.");
    m.def("get_default_verbosity", &get_default_verbosity,
          "Return the level that a new Nix thread starts at.\n\n"
          "Before anything calls set_default_verbosity this is Nix's own "
          "compiled-in level, which is what an unconfigured session reports. "
          "Threads that Nix starts for itself, such as a substituter, log at "
          "this level.");
    m.def("set_default_verbosity", &set_default_verbosity, "level"_a,
          "Set the level that a new Nix thread starts at.\n\n"
          "Separate from set_verbosity, so that restoring one thread's level "
          "after an operation cannot undo a change meant for the whole "
          "process.");
    m.def("get_log_ceiling", &get_log_ceiling,
          "Return the level that Nix itself filters at.\n\n"
          "This is pinned at import and never changes. Nix therefore hands "
          "every message up to this level to the logger, and the logger drops "
          "what the calling thread did not ask for. NANOPYNIX_LOG_CEILING "
          "lowers the pin, which makes Nix drop the message earlier and saves "
          "the cost of formatting it.");

    // ── Terminal utilities (no init required) ───────────────────
    //
    // Nix writes the escape sequences that reach us, so Nix owns the answer to
    // which byte is an escape sequence. `filterANSIEscapes` is that answer, it
    // lives in libutil, and it reads no configuration and no global state.
    m.def("filter_ansi_escapes",
          [](const std::string &s, bool filter_all, std::optional<unsigned int> width) -> std::string {
            return nix::filterANSIEscapes(s, filter_all,
                                          width.value_or(std::numeric_limits<unsigned int>::max()));
          },
          "s"_a, "filter_all"_a = false, "width"_a = nb::none(),
          "Filter the ANSI escape sequences out of *s*, the way Nix does.\n\n"
          "With *filter_all* false, a colour sequence stays and counts for no "
          "width. With *filter_all* true, every sequence goes, which includes "
          "the OSC 8 hyperlinks that a plain SGR pattern leaves behind. A tab "
          "becomes spaces to the next multiple of eight either way, and a "
          "carriage return and a bell go.\n\n"
          "*width* truncates the result to that many printable characters, "
          "which counts a wide character as two. None does not truncate.");

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
