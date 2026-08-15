#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <nanobind/nanobind.h>

#include <nix/expr/eval.hh>
#include <nix/expr/eval-settings.hh>
#include <nix/expr/value.hh>
#include <nix/fetchers/fetch-settings.hh>
#include <nix/store/store-api.hh>
#include <nix/util/ref.hh>

#include "nanopynix_gc_thread.hh"

struct PyValue;

// `traceable_allocator` is at global scope in both builds, and needs no
// `#if` here. With the collector it comes from `<gc/gc_allocator.h>`, which
// `nix/expr/eval-gc.hh` includes; without it, that same header defines a shim
// of `std::allocator` under the same name and also at global scope. Writing
// `nix::traceable_allocator` compiles with the collector, because the name is
// then found in the enclosing global scope, and fails without it. Only the
// `-nogc` variant catches that, and it did.

struct PyEvalState {
    using SettingsMap = std::map<std::string, std::string>;

    std::shared_ptr<nix::Store> store;
    std::shared_ptr<nix::Store> build_store;
    bool _readOnlyMode = false;
    nix::fetchers::Settings fetchSettings;
    nix::EvalSettings evalSettings{_readOnlyMode};
    std::shared_ptr<nix::EvalState> state;
    std::shared_ptr<bool> alive = std::make_shared<bool>(true);

    /// The thread that built this evaluator, and the only one that may drive
    /// it. A default member initializer, so it is set before the constructor
    /// body calls `init`.
    const std::thread::id owner = std::this_thread::get_id();
    std::shared_ptr<nix::StaticEnv> repl_static_env;

    /// The root that keeps the REPL environment alive, and the only one.
    ///
    /// `begin_repl` allocates one `nix::Env` with `GC_MALLOC`, and `repl_env`
    /// below is a plain pointer inside this object. A `PyEvalState` lives in
    /// the Python heap, and Boehm scans no part of that heap, so a plain
    /// pointer roots nothing: the collector frees the environment while the
    /// REPL scope is open, and the block comes back as something else.
    ///
    /// `traceable_allocator` allocates with `GC_MALLOC_UNCOLLECTABLE`, which
    /// Boehm treats as a root and scans. This is how `nix::EvalState` roots
    /// its own base environment -- `baseEnvP` in `src/libexpr/eval.cc` is the
    /// same `std::allocate_shared<Env *>` -- and how `nix::allocRootValue`
    /// roots a value. Upstream's REPL takes the other route and derives
    /// `NixRepl` from `gc`, which puts the whole object in the collector's
    /// heap. Either roots it; a raw member of a Python object does not.
    ///
    /// Measured, on the reproduction that `docs/collector-and-threads.md`
    /// gives. A finalizer on the environment, and one collection with the
    /// scope open:
    ///
    ///     state of the scope             environments collected
    ///     open, nothing evaluated                             1
    ///     open, one deep evaluation                           1
    ///
    /// Issue #70.
    std::shared_ptr<nix::Env *> repl_env_root;

    /// The REPL environment. `repl_env_root` above is what keeps it alive.
    nix::Env *repl_env = nullptr;
    size_t repl_displ = 0;
    /// How many bindings `repl_env` has room for, remembered from the
    /// `allocEnv` call in `begin_repl` that made it.
    ///
    /// Every bounds check reads this rather than a constant of its own.
    /// `nix::Env::values` is a flexible array with no length, so a guard that
    /// disagrees with the allocation does not fail -- it writes past the end.
    /// Three of the four sites used to spell 32768 literally while the
    /// allocation used a `constexpr` scoped inside `begin_repl`, which is
    /// exactly the drift this removes: there is now one value, and the guards
    /// read the one that was passed to `allocEnv`.
    size_t repl_env_capacity = 0;

    using EvalSettingsConfigurator = std::function<void(nix::EvalSettings &)>;

    static std::vector<EvalSettingsConfigurator> &evalSettingsConfigurators() {
        static std::vector<EvalSettingsConfigurator> configurators;
        return configurators;
    }

    PyEvalState(std::shared_ptr<nix::Store> s,
                const std::vector<std::string> &searchPath = {},
                std::shared_ptr<nix::Store> buildStore = nullptr,
                const SettingsMap &evalSettingsOverrides = {},
                const SettingsMap &fetchSettingsOverrides = {})
        : store(std::move(s)), build_store(std::move(buildStore))
    {
        init(searchPath, evalSettingsOverrides, fetchSettingsOverrides);
    }

    PyEvalState(nix::Store &s, const std::vector<std::string> &searchPath = {},
                const SettingsMap &evalSettingsOverrides = {},
                const SettingsMap &fetchSettingsOverrides = {})
        : store(s.shared_from_this()), build_store(store)
    {
        init(searchPath, evalSettingsOverrides, fetchSettingsOverrides);
    }

    ~PyEvalState() {
        if (alive)
            *alive = false;
    }

    /// Refuse an operation that does not come from the owning thread.
    ///
    /// The Boehm collector neither scans nor suspends a thread it does not
    /// know, and an evaluator allocates in the collected heap, and writes
    /// pointers into it, on whichever thread drives it. So a foreign thread
    /// is not an unlikely race. It is a stack the collector cannot see, and a
    /// heap it can mutate under a collection. Nothing said no before this,
    /// and a foreign thread returned correct answers -- see issue #30.
    ///
    /// The shape copies `alive`, which turns a use-after-free into an
    /// exception. This turns silent corruption into an exception.
    ///
    /// Destruction is deliberately not guarded. `~PyValue` runs wherever the
    /// last Python reference dies, which is usually not this thread, and it
    /// only frees a Boehm root. `GC_free` needs no registration.
    void checkThread() const {
        const auto here = std::this_thread::get_id();
        if (here == owner)
            return;
        std::ostringstream message;
        message << "this evaluator belongs to thread " << owner
                << ", so thread " << here << " cannot use it. Nix confines an "
                << "EvalState, and every value of that EvalState, to the "
                << "thread that built it.";
        throw std::runtime_error(message.str());
    }

    /// Apply one registered eval setting to this instance's own EvalSettings.
    /// Construction-time-snapshotted settings (nix-path, pure-eval,
    /// restrict-eval, the profiler settings) only take effect when passed to
    /// the constructor; the rest may be changed at any time.
    void set_eval_setting(const std::string &name, const std::string &value) {
        checkThread();
        if (!evalSettings.set(name, value))
            throw std::runtime_error("unknown eval setting: " + name);
    }

    /// Apply one registered fetch setting to this instance's own fetchers::Settings.
    void set_fetch_setting(const std::string &name, const std::string &value) {
        checkThread();
        if (!fetchSettings.set(name, value))
            throw std::runtime_error("unknown fetch setting: " + name);
    }

    PyValue eval_string(const std::string &expr, const std::string &path = "<string>");
    PyValue eval_file(const std::string &path);
    void begin_repl();
    bool repl_active() const;
    PyValue repl_eval_string(const std::string &expr, const std::string &path = "<string>");
    PyValue repl_eval_file(const std::string &path);
    PyValue repl_load_file(const std::string &path);
    std::optional<PyValue> repl_process_line(const std::string &line, const std::string &path = "<string>");
    std::vector<std::string> repl_add_attrs(PyValue attrs);
    std::vector<std::string> repl_scope_names() const;

    /// The evaluation statistics of this evaluator, as a JSON document.
    ///
    /// `nix` the command writes the same report when `NIX_SHOW_STATS` is set,
    /// and it writes it to stderr or to a file. An embedded evaluator reads
    /// neither, so `nix-2.35-count-calls.patch` splits a `statisticsJSON` out
    /// of `EvalState::printStatistics`. That patch reaches 2.34 and every
    /// later version, and it does not reach 2.31.
    std::string statistics_json() const;
    void reset_file_cache();
    PyValue alloc_value();

    /// Convert a JSON-compatible Python object into a Nix Value.
    PyValue value_from_python(nanobind::object obj);

private:
    /// Bind `value` to `symbol` at the next free displacement in the REPL env,
    /// shadowing any existing binding of that name.
    ///
    /// Callers must have checked that the REPL scope is active -- this reads
    /// `repl_env` and `repl_static_env` without guarding them, because both of
    /// its call sites are in `repl_process_line`, which refuses at entry.
    /// Throws `std::runtime_error` when the env is full.
    void repl_bind(nix::Symbol symbol, nix::Value &value);

    void init(const std::vector<std::string> &searchPath,
              const SettingsMap &evalSettingsOverrides,
              const SettingsMap &fetchSettingsOverrides) {
        // **Before anything here allocates in the collected heap.** `owner`
        // above says which thread may drive this evaluator; these two say that
        // a collector exists, and that it knows that thread. A caller that
        // never touches `NixThreadExecutor` -- `nanopynix.EvalState(store)` on
        // the thread it happens to be on -- reaches the collector through
        // these lines alone.
        //
        // **In this order, and both aborts are measured.** Issue #54.
        // Without the first line, `nanopynix.EvalState(store)` in a process
        // where nothing called `init_libexpr` dies on SIGABRT with bdwgc's
        // "Threads explicit registering is not previously enabled":
        // `GC_allow_register_threads` runs inside `GC_INIT`, so the second
        // line cannot come first. Behind that one waits Nix's own
        // `assertGCInitialized()` in the `EvalState` constructor, a bare
        // `assert`. Neither is catchable, and one call answers both.
        nanopynix_start_gc_owner_thread();
        nanopynix_ensure_gc_thread_registered();

        for (auto &cfg : evalSettingsConfigurators())
            cfg(evalSettings);

        for (auto &[name, value] : evalSettingsOverrides)
            set_eval_setting(name, value);
        for (auto &[name, value] : fetchSettingsOverrides)
            set_fetch_setting(name, value);

        nix::LookupPath lookupPath;
        for (auto &entry : searchPath) {
            auto eq = entry.find('=');
            if (eq != std::string::npos) {
                nix::LookupPath::Elem elem{
                    .prefix = nix::LookupPath::Prefix(entry.substr(0, eq)),
                    .path = nix::LookupPath::Path{entry.substr(eq + 1)}
                };
                lookupPath.elements.push_back(std::move(elem));
            } else {
                nix::LookupPath::Elem elem{
                    .prefix = nix::LookupPath::Prefix(""),
                    .path = nix::LookupPath::Path{entry}
                };
                lookupPath.elements.push_back(std::move(elem));
            }
        }

        state = std::make_shared<nix::EvalState>(
            lookupPath,
            nix::ref<nix::Store>(store),
            fetchSettings,
            evalSettings,
            build_store ? build_store : store);
    }
};
