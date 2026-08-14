#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/shared_ptr.h>

#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <future>
#include <locale>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

#include <fcntl.h>
#include <pthread.h>
#include <unistd.h>
#ifndef __APPLE__
// Darwin ships this header but defines no `SYS_gettid`. See the three
// wrappers below for what stands in.
#include <sys/syscall.h>
#endif

// Three things this file asks the OS that Linux and Darwin answer
// differently. Each is used by the collector-owner machinery further down,
// and each has exactly one honest Darwin equivalent.
namespace {

// The calling thread's OS-level id.
//
// Used for two things only: a diagnostic line, and a non-zero sentinel
// saying the collector's owner thread has started. Neither needs the number
// to mean anything beyond "unique to this thread, stable while it runs",
// which is true of both of these.
long osThreadId() {
#ifdef __APPLE__
    // Darwin has no `gettid`. `pthread_threadid_np` is its equivalent, and
    // it is the same number `ps -M` and the debugger show.
    uint64_t tid = 0;
    pthread_threadid_np(nullptr, &tid);
    return static_cast<long>(tid);
#else
    return static_cast<long>(syscall(SYS_gettid));
#endif
}

// Whether the caller is the process's first thread.
bool isMainThread() {
#ifdef __APPLE__
    return pthread_main_np() != 0;
#else
    // On Linux the main thread is the one whose thread id equals the pid.
    return syscall(SYS_gettid) == getpid();
#endif
}

// Name the CALLING thread.
//
// Darwin's `pthread_setname_np` takes only the name and can name no thread
// but the caller's; Linux's takes the thread as well. Narrowing the wrapper
// to the calling thread is what makes the two the same function rather than
// a partial one -- and it is all any call site here wants.
void setCallingThreadName(const char *name) {
#ifdef __APPLE__
    pthread_setname_np(name);
#else
    pthread_setname_np(pthread_self(), name);
#endif
}

} // namespace

// For `NIX_USE_BOEHMGC`, which decides whether the collector exists at all in
// this build. Included before <gc/gc.h> because the macro is what gates that
// header: a `-Dgc=disabled` libexpr installs no Boehm headers, so an
// unconditional include does not compile. See `enter_evaluator_thread` below
// for what this build does instead.
#include <nix/expr/config.hh>

#if NIX_USE_BOEHMGC
#  include <gc/gc.h>
#endif

#include <nix/expr/eval.hh>
#include <nix/expr/eval-error.hh>
#include <nix/expr/eval-gc.hh>
#include <nix/expr/attr-path.hh>
#include <nix/expr/get-drvs.hh>
#include <nix/expr/value.hh>
#include <nix/expr/attr-set.hh>
#include <nix/expr/primops.hh>
#include <nix/expr/value-to-json.hh>
#include <nix/fetchers/fetch-to-store.hh>
#include <nix/fetchers/tarball.hh>
#include <nix/flake/flakeref.hh>
#include <nix/flake/settings.hh>
#include <nix/store/build-result.hh>
#include <nix/store/derived-path.hh>
#include <nix/util/experimental-features.hh>
#include <nix/util/file-system.hh>
#include <nix/util/logging.hh>
#include <nix/util/util.hh>

#include <algorithm>
#include <unordered_set>

#include <nlohmann/json.hpp>

#include <nanopynix/nix_compat_config.hh>

#include "build_result_util.hh"
#include "nanopynix_errors.hh"
#include "nix_error_info.hh"
#include "py_value.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// PyValue out-of-line method implementations
// =========================================================================

std::string PyValue::type_name() {
    auto *v = checkedValue();
    switch (v->type()) {
        case nix::nThunk:    return "thunk";
        case nix::nInt:      return "int";
        case nix::nFloat:    return "float";
        case nix::nBool:     return "bool";
        case nix::nString:   return "string";
        case nix::nPath:     return "path";
        case nix::nNull:     return "null";
        case nix::nAttrs:    return "attrs";
        case nix::nList:     return "list";
        case nix::nFunction: return "function";
        case nix::nExternal: return "external";
        default:             return "unknown";
    }
}

bool PyValue::is_null()     const { return checkedValue()->type() == nix::nNull; }
bool PyValue::is_int()      const { return checkedValue()->type() == nix::nInt; }
bool PyValue::is_float()    const { return checkedValue()->type() == nix::nFloat; }
bool PyValue::is_bool()     const { return checkedValue()->type() == nix::nBool; }
bool PyValue::is_string()   const { return checkedValue()->type() == nix::nString; }
bool PyValue::is_path()     const { return checkedValue()->type() == nix::nPath; }
bool PyValue::is_attrs()    const { return checkedValue()->type() == nix::nAttrs; }
bool PyValue::is_list()     const { return checkedValue()->type() == nix::nList; }
bool PyValue::is_function() const { return checkedValue()->type() == nix::nFunction; }
bool PyValue::is_thunk()    const { return checkedValue()->type() == nix::nThunk; }

// The as_* accessors below MUST go through EvalState::force*, never through
// nix::Value's raw integer()/fpoint()/boolean()/c_str().
//
// Those raw accessors are `getStorage<T>()` -- an unchecked read of the value's
// union (nix/expr/value.hh:1328-1371). Reading the wrong alternative is
// undefined behaviour, and for `c_str()` specifically it dereferences whatever
// bits the other alternative left behind: `as_string()` on an int used to
// segfault the interpreter outright. The numeric ones were quieter and no
// better, returning garbage for a value of the wrong type.
//
// `checkedValue()` does not help -- it validates that the *root* is still
// alive, not that the value holds what the caller is asking for.
//
// EvalState::force* forces a thunk first and then type-checks, raising
// nix::TypeError (bound, and carrying its ErrorInfo via nix_error_info.hh) for
// a mismatch. That is also why these no longer need an is-it-a-thunk dance:
// the raw accessors were unsafe on unforced values too.

nix::EvalState &PyValue::requireEvalState() const {
    auto *es = evalState();
    if (!es)
        throw std::runtime_error("Nix value has no evaluator; it cannot be read");
    return *es;
}

int64_t PyValue::as_int() const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    nb::gil_scoped_release release;
    return static_cast<int64_t>(es.forceInt(*v, nix::noPos, "while reading a Nix value as an int"));
}

double PyValue::as_float() const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    nb::gil_scoped_release release;
    return es.forceFloat(*v, nix::noPos, "while reading a Nix value as a float");
}

bool PyValue::as_bool() const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    nb::gil_scoped_release release;
    // No null -> false carve-out. This used to silently read `null` as `false`
    // for optional-flag callers, but an as_* accessor's whole contract is that
    // it raises when the type is wrong, and `null` is not a bool. Callers that
    // want the lenient reading should ask is_null() first, where the intent is
    // visible at the call site instead of hidden in the accessor.
    return es.forceBool(*v, nix::noPos, "while reading a Nix value as a bool");
}

std::string PyValue::as_string() const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    nb::gil_scoped_release release;
    // forceString, NOT forceStringNoCtx: the only thing being fixed here is the
    // unchecked union read, and rejecting string context would be a second,
    // unrelated restriction. Callers legitimately read strings that carry it --
    // a derivation's drvPath/outPath are the common case -- and the raw c_str()
    // this replaces never cared. Context is dropped rather than rejected, which
    // is what a Python-side `str` can represent; use realise_string() when the
    // referenced store paths actually need to exist.
    return std::string(es.forceString(*v, nix::noPos, "while reading a Nix value as a string"));
}

void PyValue::force() {
    if (auto *es = evalState()) {
        nb::gil_scoped_release release;
        es->forceValue(*checkedValue(), nix::noPos);
    }
}

void PyValue::force_deep() {
    if (auto *es = evalState()) {
        nb::gil_scoped_release release;
        es->forceValueDeep(*checkedValue());
    }
}

std::string PyValue::realise_string() {
    auto *es = evalState();
    if (es == nullptr) throw std::runtime_error("value has no evaluation state");
    nb::gil_scoped_release release;
    return es->realiseString(*checkedValue(), nullptr, true, nix::noPos);
}

std::vector<std::string> PyValue::realise_argv() {
    auto *es = evalState();
    if (es == nullptr) throw std::runtime_error("value has no evaluation state");
    nb::gil_scoped_release release;

    auto *value = checkedValue();
    es->forceList(*value, nix::noPos, "while evaluating the argument passed to :exec");

    nix::NixStringContext context;
    std::vector<std::string> argv;
    for (auto *element : value->listView()) {
        argv.emplace_back(es->coerceToString(
            nix::noPos,
            *element,
            context,
            "while evaluating an element of the argument passed to :exec",
            false,
            false).toOwned());
    }
    auto rewrites = es->realiseContext(context);
    for (auto &argument : argv)
        argument = nix::rewriteStrings(argument, rewrites);
    return argv;
}

nb::dict PyValue::edit_location() {
    std::optional<nix::SourcePath> source_path;
    uint32_t line = 0;
    // Hoisted out of the block below only so the last failure branch, which
    // runs after it, can also report through nix::EvalError.
    auto *es = evalState();
    if (es == nullptr) throw std::runtime_error("value has no evaluation state");
    {
        nb::gil_scoped_release release;
        auto *value = checkedValue();
        if (value->type() == nix::nPath || value->type() == nix::nString) {
            nix::NixStringContext context;
            source_path = es->coerceToPath(nix::noPos, *value, context, "while evaluating the filename to edit");
        } else if (value->isLambda()) {
            auto pos = es->positions[value->lambda().fun->pos];
            if (auto path = std::get_if<nix::SourcePath>(&pos.origin)) {
                source_path = *path;
                line = pos.line;
            } else {
                throw nix::EvalError(*es, "selected function cannot be shown in an editor");
            }
        } else {
            auto location = nix::findPackageFilename(*es, *value, "selected value");
            source_path = std::move(location.first);
            line = location.second;
        }
    }
    if (!source_path)
        throw nix::EvalError(*es, "could not determine source location");
    auto physical_path = source_path->getPhysicalPath();
    if (!physical_path)
        throw nix::EvalError(*es, "cannot open '%s' in an editor because it has no physical path", *source_path);
    nb::dict result;
    result["path"] = physical_path->string();
    result["line"] = line;
    return result;
}

// to_python and to_json are implemented below.

// The five navigation accessors below all go through nix's own forceAttrs /
// forceList rather than testing `v->type()` themselves.
//
// Two bugs at once. They used to answer for the wrong type instead of
// refusing: list_length() returned 0 for an attrset, attr_names() returned {}
// for an int, has_attr() returned false for a function. A plausible answer is
// worse than an exception -- `for i in range(await v.list_length())` on an
// attrset is a silent no-op that surfaces as wrong output much later. The two
// that did refuse threw std::runtime_error, which lands outside the NixError
// hierarchy on the Python side.
//
// And none of them forced first, so an unforced thunk read as "not a list" and
// took the same silent path.
//
// forceAttrs/forceList fix all three: they force, they raise nix::TypeError
// with nix's own message, and that maps to NixTypeError like every other
// type mismatch in the bindings.
size_t PyValue::list_length() const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    {
        nb::gil_scoped_release release;
        es.forceList(*v, nix::noPos, "while reading the length of a Nix list");
    }
    return v->listSize();
}

PyValue PyValue::list_get(size_t idx) const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    {
        nb::gil_scoped_release release;
        es.forceList(*v, nix::noPos, "while indexing a Nix list");
    }
    auto size = v->listSize();
    if (idx >= size)
        // Wording follows Nix's own out-of-range report (prim_elemAt: "called
        // with index 99 on a list of size 2") rather than Python's, since the
        // subject is a Nix list. The Python-ness is carried by the exception
        // *class* -- ListIndexError is an IndexError as well as a NixError.
        throw nanopynix::ListIndexError(
            es, "index %d is out of bounds for a list of size %d", idx, size);
    auto *elem = v->listView()[idx];
    {
        nb::gil_scoped_release release;
        es.forceValue(*elem, nix::noPos);
    }
    return PyValue(elem, eval, eval_alive);
}

std::vector<std::string> PyValue::attr_names() const {
    std::vector<std::string> names;
    auto *v = checkedValue();
    auto &es = requireEvalState();
    {
        nb::gil_scoped_release release;
        es.forceAttrs(*v, nix::noPos, "while listing the attributes of a Nix value");
    }
    for (auto &attr : *v->attrs())
        names.push_back(std::string(es.symbols[attr.name]));
    return names;
}

bool PyValue::has_attr(const std::string &name) const {
    auto *v = checkedValue();
    auto &es = requireEvalState();
    {
        nb::gil_scoped_release release;
        es.forceAttrs(*v, nix::noPos, "while testing for a Nix attribute");
    }
    auto sym = es.symbols.create(name);
    for (auto &attr : *v->attrs())
        if (attr.name == sym) return true;
    return false;
}

PyValue PyValue::attr_get(const std::string &name) const {
    auto *value = checkedValue();
    auto &es = requireEvalState();
    {
        nb::gil_scoped_release release;
        es.forceAttrs(*value, nix::noPos, "while selecting a Nix attribute");
    }
    auto sym = es.symbols.create(name);
    for (auto &attr : *value->attrs()) {
        if (attr.name == sym) {
            nix::Value *v;
            {
                nb::gil_scoped_release release;
                v = es.allocValue();
                es.forceValue(*attr.value, nix::noPos);
            }
            *v = *attr.value;
            return PyValue(v, eval, eval_alive);
        }
    }
    // Same wording and the same "Did you mean ...?" ranking Nix uses for
    // `{ foo = 1; }.fooo`, built from this attrset's own symbol table. The
    // candidate names exist only here, so the suggestions have to be computed
    // here too -- Python would have to be handed every attribute name to do
    // the same job.
    nix::StringSet candidates;
    for (auto &attr : *value->attrs())
        candidates.insert(std::string(es.symbols[attr.name]));
    nanopynix::MissingAttributeError error(es, "attribute '%s' missing", name);
    error.with_suggestions(nix::Suggestions::bestMatches(candidates, name));
    throw error;
}

PyValue PyValue::auto_call() {
    auto *es = evalState();
    nix::Value *result;
    {
        nb::gil_scoped_release release;
        result = es->allocValue();
        auto attrs = es->buildBindings(0);
        es->autoCallFunction(*attrs.alreadySorted(), *checkedValue(), *result);
        es->forceValue(*result, nix::noPos);
    }
    return PyValue(result, eval, eval_alive);
}

PyValue PyValue::call(PyValue arg) {
    auto *es = evalState();
    nix::Value *result;
    {
        nb::gil_scoped_release release;
        result = es->allocValue();
        es->callFunction(*checkedValue(), *arg.checkedValue(), *result, nix::noPos);
        es->forceValue(*result, nix::noPos);
    }
    return PyValue(result, eval, eval_alive);
}

std::string PyValue::derived_path() {
    auto *es = evalState();
    auto *v = checkedValue();

    std::optional<nix::StorePath> drv_path;
    {
        nb::gil_scoped_release release;
        auto package_info = nix::getDerivation(*es, *v, false);
        if (!package_info)
            throw nix::EvalError(*es, "selected value is not a derivation");
        auto maybe_drv_path = package_info->queryDrvPath();
        if (!maybe_drv_path)
            throw nix::EvalError(*es, "selected derivation has no drvPath");
        drv_path = std::move(*maybe_drv_path);
    }

    return eval->store->printStorePath(*drv_path);
}

nb::dict PyValue::build(
        std::shared_ptr<nix::Store> build_store,
        nix::BuildMode build_mode,
        std::shared_ptr<nix::Store> eval_store) {
    auto *es = evalState();
    auto *v = checkedValue();

    std::optional<nix::StorePath> drv_path;
    nix::PackageInfo::Outputs output_paths;
    {
        nb::gil_scoped_release release;
        auto package_info = nix::getDerivation(*es, *v, false);
        if (!package_info)
            throw nix::EvalError(*es, "selected value is not a derivation");
        drv_path = package_info->queryDrvPath();
        if (!drv_path)
            throw nix::EvalError(*es, "selected derivation has no drvPath");
        output_paths = package_info->queryOutputs(true, false);
    }

    nix::StringSet output_names;
    for (auto &[name, _path] : output_paths)
        output_names.insert(name);
    if (output_names.empty())
        output_names.insert("out");

    nb::dict outputs;
    for (auto &[name, path] : output_paths) {
        if (path)
            outputs[name.c_str()] = eval->store->printStorePath(*path);
    }

    nix::DerivedPaths paths{
        nix::DerivedPath::Built{
            .drvPath = nix::makeConstantStorePathRef(*drv_path),
            .outputs = nix::OutputsSpec::Names{output_names},
        },
    };

    auto store = build_store ? build_store : eval->store;

    std::vector<nix::KeyedBuildResult> results;
    try {
        nb::gil_scoped_release release;
        results = store->buildPathsWithResults(paths, build_mode, eval_store);
    } catch (nix::Error &e) {
        nix::logger->logEI(nix::lvlError, e.info());
        e.addTrace({}, "while building evaluated derivation");
        throw;
    }

    nb::list out;
    for (auto &kbr : results) out.append(nanopynix::build_result::from_kbr(kbr, *store));
    nb::dict response;
    response["drv_path"] = eval->store->printStorePath(*drv_path);
    response["outputs"] = outputs;
    response["results"] = out;
    return response;
}

std::string PyValue::repr() {
    return "PyValue(" + type_name() + ")";
}

// =========================================================================
// File-argument resolution
// =========================================================================

/// Resolve a user-supplied file argument the way the `nix` CLI does: a
/// pseudo-URL is downloaded, a `flake:` reference is fetched, `<name>` goes
/// through the lookup path, and anything else is an ordinary path.
///
/// This is `nix::lookupFileArg` from libcmd, reimplemented here rather than
/// called, for two reasons.
///
/// The first is correctness, and it is not cosmetic. libcmd's version resolves
/// the `flake:` branch through `nix::fetchSettings` -- a *process-global*
/// `fetchers::Settings` that libcmd defines and the `nix` binary configures
/// from its command line. nanopynix has no such global: every `PyEvalState`
/// carries its own `fetchSettings` (see py_eval.hh), which is what
/// `set_fetch_setting` writes to and what the constructor's
/// `fetchSettingsOverrides` populate. Calling libcmd meant a `flake:` argument
/// silently ignored all of that and fetched with process-wide defaults, while
/// every other path through the bindings honoured the session's. Using
/// `state.fetchSettings` throughout is the whole point.
///
/// The second is that this was the *only* symbol nanopynix took from libcmd,
/// and libcmd drags lowdown and editline -- a Markdown renderer and a line
/// editor -- into the address space of every Python process that imports
/// nanopynix. Twenty-five lines is a better trade than a REPL's dependencies.
///
/// The `baseDir` parameter of the original is dropped: all three call sites
/// passed none, and `absPath` already resolves against the process's working
/// directory in that case.
static nix::SourcePath lookup_file_arg(nix::EvalState &state, std::string_view s) {
    if (nix::EvalSettings::isPseudoUrl(s)) {
        auto accessor = nix::fetchers::downloadTarball(
            *state.store,
            state.fetchSettings, nix::EvalSettings::resolvePseudoUrl(s));
        auto storePath = nix::fetchToStore(
            state.fetchSettings, *state.store, nix::SourcePath(accessor), nix::FetchMode::Copy);
        return state.storePath(storePath);
    }

    if (nix::hasPrefix(s, "flake:")) {
        nix::experimentalFeatureSettings.require(nix::Xp::Flakes);
        auto flakeRef =
            nix::parseFlakeRef(state.fetchSettings, std::string(s.substr(6)), {}, true, false);
        auto [accessor, lockedRef] = flakeRef.resolve(state.fetchSettings, *state.store)
                                         .lazyFetch(state.fetchSettings, *state.store);
        auto storePath = nix::fetchToStore(
            state.fetchSettings, *state.store, nix::SourcePath(accessor), nix::FetchMode::Copy,
            lockedRef.input.getName());
        state.allowPath(storePath);
        return state.storePath(storePath);
    }

    if (s.size() > 2 && s.front() == '<' && s.back() == '>')
        return state.findFile(std::string(s.substr(1, s.size() - 2)));

    return state.rootPath(nix::absPath(std::filesystem::path{s}).string());
}

// =========================================================================
// PyEvalState out-of-line methods
// =========================================================================

PyValue PyEvalState::eval_string(const std::string &expr, const std::string &path) {
    checkThread();
    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto *parsedExpr = state->parseExprFromString(
            expr, state->rootPath(nix::CanonPath(path)));
        v = state->allocValue();
        state->eval(parsedExpr, *v);
        state->forceValue(*v, nix::noPos);
    }
    return PyValue(v, this, alive);
}

void PyEvalState::begin_repl() {
    checkThread();
    if (repl_env != nullptr)
        throw std::runtime_error("REPL scope is already active");

    constexpr size_t repl_env_size = 32768;
    repl_static_env = std::make_shared<nix::StaticEnv>(nullptr, state->staticBaseEnv);
    repl_env = &state->mem.allocEnv(repl_env_size);
    repl_env->up = &state->baseEnv;
    repl_displ = 0;
    // The one place the size is named. Every bounds check reads it back from
    // here rather than repeating the literal -- see `repl_env_capacity`.
    repl_env_capacity = repl_env_size;
}

bool PyEvalState::repl_active() const {
    checkThread();
    return repl_env != nullptr;
}

void PyEvalState::repl_bind(nix::Symbol symbol, nix::Value &value) {
    if (repl_displ >= repl_env_capacity)
        throw std::runtime_error("REPL environment is full");
    if (auto oldVar = repl_static_env->find(symbol); oldVar != repl_static_env->vars.end())
        repl_static_env->vars.erase(oldVar);
    repl_static_env->vars.emplace_back(symbol, repl_displ);
    repl_static_env->sort();
    repl_env->values[repl_displ++] = &value;
}

PyValue PyEvalState::repl_eval_string(const std::string &expr, const std::string &path) {
    checkThread();
    if (repl_env == nullptr || !repl_static_env)
        throw std::runtime_error("REPL scope is not active");

    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto *parsedExpr = state->parseExprFromString(
            expr, state->rootPath(nix::CanonPath(path)), repl_static_env);
        v = state->allocValue();
        parsedExpr->eval(*state, *repl_env, *v);
        state->forceValue(*v, nix::noPos);
    }
    return PyValue(v, this, alive);
}

PyValue PyEvalState::repl_eval_file(const std::string &path) {
    checkThread();
    if (repl_env == nullptr || !repl_static_env)
        throw std::runtime_error("REPL scope is not active");

    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto sourcePath = nix::resolveExprPath(lookup_file_arg(*state, path));
        auto *parsedExpr = state->parseExprFromFile(sourcePath, repl_static_env);
        v = state->allocValue();
        parsedExpr->eval(*state, *repl_env, *v);
    }
    return PyValue(v, this, alive);
}

PyValue PyEvalState::repl_load_file(const std::string &path) {
    checkThread();
    if (repl_env == nullptr || !repl_static_env)
        throw std::runtime_error("REPL scope is not active");

    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto sourcePath = lookup_file_arg(*state, path);
        auto *loaded = state->allocValue();
        state->evalFile(sourcePath, *loaded);
        auto autoArgs = state->buildBindings(0);
        v = state->allocValue();
        state->autoCallFunction(*autoArgs.finish(), *loaded, *v);
    }
    return PyValue(v, this, alive);
}

void PyEvalState::reset_file_cache() {
    checkThread();
    state->resetFileCache();
}

std::optional<PyValue> PyEvalState::repl_process_line(const std::string &line, const std::string &path) {
    checkThread();
    if (repl_env == nullptr || !repl_static_env)
        throw std::runtime_error("REPL scope is not active");

    nb::gil_scoped_release release;
    auto basePath = state->rootPath(nix::CanonPath(path));

    nix::ExprAttrs *bindings = nullptr;
    try {
        bindings = state->parseReplBindings(line, basePath, repl_static_env);
    } catch (nix::ParseError &) {
        try {
            bindings = state->parseReplBindings(line + ";", line, basePath, repl_static_env);
        } catch (nix::ParseError &) {
            auto *parsedExpr = state->parseExprFromString(line, basePath, repl_static_env);
            auto *value = state->allocValue();
            parsedExpr->eval(*state, *repl_env, *value);
            state->forceValue(*value, nix::noPos);
            return PyValue(value, this, alive);
        }
    }

    auto *inheritEnv = bindings->inheritFromExprs
        ? bindings->buildInheritFromEnv(*state, *repl_env)
        : nullptr;
    for (auto &[symbol, def] : *bindings->attrs) {
        nix::Value &value(*state->allocValue());
        value.mkThunk(def.chooseByKind(repl_env, repl_env, inheritEnv), def.e);
        repl_bind(symbol, value);
    }
    return std::nullopt;
}

std::vector<std::string> PyEvalState::repl_add_attrs(PyValue attrs) {
    checkThread();
    if (repl_env == nullptr || !repl_static_env)
        throw std::runtime_error("REPL scope is not active");

    nb::gil_scoped_release release;
    auto *value = attrs.checkedValue();
    state->forceAttrs(*value, nix::noPos, "while evaluating an attribute set to be merged in the REPL scope");
    auto *bindings = value->attrs();
    // `>`, not `>=`: this fills displacements `repl_displ` through
    // `repl_displ + size - 1`, so the last one is in range exactly when
    // `repl_displ + size <= capacity`. Written with `>=` it rejected a batch
    // that would have filled the env precisely to the end -- conservative
    // rather than unsafe, but one short of `repl_bind`'s single-binding check,
    // which this is the batch form of. Not routed through `repl_bind` itself:
    // that shadows per binding, while this sorts and deduplicates once at the
    // end, which is a different operation and a much cheaper one.
    if (repl_displ + bindings->size() > repl_env_capacity)
        throw std::runtime_error("REPL environment is full");

    std::vector<std::string> names;
    names.reserve(bindings->size());
    for (auto &attr : *bindings) {
        repl_static_env->vars.emplace_back(attr.name, repl_displ);
        repl_env->values[repl_displ++] = attr.value;
        names.emplace_back(state->symbols[attr.name]);
    }
    repl_static_env->sort();
    repl_static_env->deduplicate();
    return names;
}

std::string PyEvalState::statistics_json() const {
    checkThread();
    // `statisticsJSON` walks the counter tables of the evaluator, and the
    // report of a large evaluation is not small. The GIL is not needed for
    // either, so a second thread keeps running while this one builds it.
    nb::gil_scoped_release release;
    return state->statisticsJSON();
}

std::vector<std::string> PyEvalState::repl_scope_names() const {
    checkThread();
    if (repl_env == nullptr || !repl_static_env)
        throw std::runtime_error("REPL scope is not active");

    nb::gil_scoped_release release;
    std::unordered_set<std::string> seen;
    std::vector<std::string> names;
    for (std::shared_ptr<const nix::StaticEnv> env = repl_static_env; env != nullptr; env = env->up) {
        for (const auto &[symbol, _displacement] : env->vars) {
            auto name = std::string(state->symbols[symbol]);
            if (seen.insert(name).second)
                names.push_back(std::move(name));
        }
    }
    std::sort(names.begin(), names.end());
    return names;
}

PyValue PyEvalState::alloc_value() {
    checkThread();
    auto *value = state->allocValue();
    value->mkNull();
    return PyValue(value, this, alive);
}

PyValue PyEvalState::eval_file(const std::string &path) {
    checkThread();
    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto sourcePath = lookup_file_arg(*state, path);
        v = state->allocValue();
        state->evalFile(sourcePath, *v);
    }
    // Do NOT force — the caller may want to navigate lazily.
    return PyValue(v, this, alive);
}

// =========================================================================
// PyValue::evalState
// =========================================================================

nix::EvalState *PyValue::evalState() const {
    if (!eval_alive || !*eval_alive || eval == nullptr)
        throw std::runtime_error("EvalState has been released");
    // The one funnel for every accessor of a value: each one reaches this
    // through `checkedValue` or `requireEvalState`. See
    // `PyEvalState::checkThread` for why a foreign thread is refused, and for
    // why the destructor is not guarded.
    eval->checkThread();
    return eval->state.get();
}

nix::Value *PyValue::checkedValue() const {
    (void) evalState();
    if (!root || !*root)
        throw std::runtime_error("Nix value has been released");
    return *root;
}

// =========================================================================
// PyValue::to_python
// =========================================================================

// Scalars are converted directly; everything compound is handed to Nix's own
// printValueAsJSON (see to_json below).
//
// The compound cases used to be a hand-rolled recursive walk over attrs and
// list elements. That walk was a reimplementation of printValueAsJSON minus
// every rule that makes it terminate, and it did not survive contact with a
// derivation: a derivation's `out`/`all`/`drvAttrs` point back at the
// derivation, so the walk recursed until the C++ stack ran out and the process
// took SIGSEGV. Nix stops there instead -- an attrset with `__toString` becomes
// that string, an attrset with `outPath` becomes the output path, so a
// derivation converts to its store path (confirmed against `nix eval --json`)
// -- and a genuine non-derivation cycle raises Nix's own "max-call-depth
// exceeded" rather than killing the interpreter.
//
// The scalar cases stay direct because the rpc worker converts every scalar
// leaf through here one at a time, and routing an int through a json round trip
// to get the same int back is pure overhead. copy_to_store is false so nPath
// keeps rendering as a literal filesystem path.
nb::object PyValue::to_python() {
    auto *es = evalState();
    auto *value = checkedValue();

    force();

    switch (value->type()) {
        case nix::nNull:
            return nb::none();
        case nix::nInt:
            return nb::int_(static_cast<int64_t>(value->integer()));
        case nix::nFloat:
            return nb::float_(value->fpoint());
        case nix::nBool:
            return nb::bool_(value->boolean());
        case nix::nString: {
            std::string string;
            {
                nb::gil_scoped_release release;
                // forceString, NOT forceStringNoCtx, for the same reason as
                // as_string() above: an interpolated derivation path
                // ("${drv}") is an ordinary string that happens to carry
                // store-path context, and refusing to convert it would make
                // to_python() unable to read the single most common shape of
                // Nix string there is. The context is dropped because a Python
                // str cannot carry one; callers that need it must stay on the
                // Nix side (Value.apply, or realise the path).
                string = es->forceString(*value, nix::noPos, "while converting a Nix string to Python");
            }
            return nb::str(string.c_str());
        }
        case nix::nPath: {
            nix::NixStringContext ctx;
            std::string path;
            {
                nb::gil_scoped_release release;
                path = es->coerceToPath(nix::noPos, *value, ctx, "").path.abs();
            }
            return nb::str(path.c_str());
        }
        default:
            // nList, nAttrs, and the types Nix refuses to flatten at all
            // (nFunction, nExternal). Previously the default arm returned the
            // *name* of the type as a string, so a lambda silently converted to
            // the string "function"; now it raises whatever printValueAsJSON
            // raises, which is Nix's own error.
            return to_json(/*copy_to_store=*/false);
    }
}

// =========================================================================
// PyValue::to_json
// =========================================================================

static nb::object json_to_python(const nlohmann::json &j) {
    switch (j.type()) {
        case nlohmann::json::value_t::null:
            return nb::none();
        case nlohmann::json::value_t::boolean:
            return nb::bool_(j.get<bool>());
        case nlohmann::json::value_t::number_integer:
        case nlohmann::json::value_t::number_unsigned:
            return nb::int_(j.get<int64_t>());
        case nlohmann::json::value_t::number_float:
            return nb::float_(j.get<double>());
        case nlohmann::json::value_t::string:
            return nb::str(j.get<std::string>().c_str());
        case nlohmann::json::value_t::array: {
            nb::list list;
            for (const auto &elem : j)
                list.append(json_to_python(elem));
            return list;
        }
        case nlohmann::json::value_t::object: {
            nb::dict dict;
            for (auto it = j.begin(); it != j.end(); ++it)
                dict[nb::str(it.key().c_str())] = json_to_python(it.value());
            return dict;
        }
        default:
            return nb::none();
    }
}

nb::object PyValue::to_json(bool copy_to_store) {
    auto *es = evalState();
    auto *value = checkedValue();

    nix::NixStringContext context;
    nlohmann::json j;
    {
        nb::gil_scoped_release release;
        j = nix::printValueAsJSON(*es, true, *value, nix::noPos, context, copy_to_store);
    }
    return json_to_python(j);
}

// =========================================================================
// evalFile
// =========================================================================

static PyValue eval_file_impl(PyEvalState &es, const std::string &path) {
    return es.eval_file(path);
}

// =========================================================================
// PrimOp — register Python functions as Nix builtins
// =========================================================================

// Convert a nix::Value* (already forced) to a Python object, recursively.
// `context` accumulates the NixStringContext of every string encountered
// anywhere in the argument tree (one shared outparam, unioned across the
// whole call) -- py_primop_bridge re-attaches it to whatever the primop
// returns, so e.g. a derivation reference embedded three attrsets deep
// still keeps the resulting output's closure correct. This is a
// deliberate over-approximation: a primop's return value gets the context
// of its *entire* input, not just the parts it actually used, but
// under-attaching is a real closure/`nix copy` correctness bug (silently
// missing a runtime dependency) while over-attaching just keeps something
// alive slightly longer than strictly necessary -- L2 (this in-process
// bridge) is exactly the layer that can afford to always do this since it
// already has live C++ Value access; a caller that explicitly wants to
// drop context can still reach for builtins.unsafeDiscardStringContext.
static nb::object value_to_python_arg(
    nix::EvalState &state, nix::Value *v, const std::string &primop_name,
    nix::NixStringContext &context
) {
    if (!v) return nb::none();
    switch (v->type()) {
        case nix::nNull:   return nb::none();
        case nix::nInt:    return nb::int_(static_cast<int64_t>(v->integer()));
        case nix::nFloat:  return nb::float_(v->fpoint());
        case nix::nBool:   return nb::bool_(v->boolean());
        case nix::nString: {
            // coerceToString (not forceStringNoCtx/realiseString) so a
            // string carrying string context -- e.g. "${someDerivation}"
            // -- still yields the real, final store path text (ordinary
            // input-addressed derivations have a path computable without
            // building) while accumulating the touched context into
            // `context` instead of discarding it. realiseContext then
            // builds whatever was referenced and resolves any
            // content-addressed placeholder to its concrete path -- same
            // build-and-substitute guarantee the old realiseString call
            // gave (see test_string_arg_with_context_is_realised: Python
            // primop implementations may read the referenced path's
            // content, so it must already exist on disk), just without
            // throwing away what was realised.
            std::string string;
            {
                nb::gil_scoped_release release;
                string = std::string(state.coerceToString(
                    nix::noPos, *v, context,
                    "while evaluating a string passed to a Python primop",
                    false, false).toOwned());
                auto rewrites = state.realiseContext(context);
                string = nix::rewriteStrings(string, rewrites);
            }
            return nb::str(string.c_str());
        }
        case nix::nPath: {
            nix::NixStringContext ctx;
            std::string path;
            {
                nb::gil_scoped_release release;
                path = state.coerceToPath(nix::noPos, *v, ctx, "").path.abs();
            }
            return nb::str(path.c_str());
        }
        case nix::nList: {
            auto list = nb::list();
            auto n = v->listSize();
            auto lv = v->listView();
            for (size_t i = 0; i < n; i++) {
                {
                    nb::gil_scoped_release release;
                    state.forceValue(*lv[i], nix::noPos);
                }
                list.append(value_to_python_arg(state, lv[i], primop_name, context));
            }
            return list;
        }
        case nix::nAttrs: {
            auto dict = nb::dict();
            if (auto *bindings = v->attrs()) {
                for (auto &attr : *bindings) {
                    {
                        nb::gil_scoped_release release;
                        state.forceValue(*attr.value, nix::noPos);
                    }
                    dict[nb::str(std::string(state.symbols[attr.name]).c_str())] =
                        value_to_python_arg(state, attr.value, primop_name, context);
                }
            }
            return dict;
        }
        default:
            state.error<nix::TypeError>(
                "%s: argument contains non JSON-compatible Nix value of type '%s'",
                primop_name, showType(v->type())).debugThrow();
    }
}

// Ownership container for anonymous primops created from Python callables.
// These live for the lifetime of the process (matches nix::PrimOp lifetime).
static std::vector<std::unique_ptr<nix::PrimOp>> &anon_primops() {
    static std::vector<std::unique_ptr<nix::PrimOp>> ops;
    return ops;
}

// Forward declaration — py_primop_bridge is defined later but called
// from the callable branch in python_to_value.
static void py_primop_bridge(
    const std::string &name, int arity,
    nix::EvalState &state, const nix::PosIdx,
    nix::Value **args, nix::Value &ret);

// Holder for a registered Python primop callback (forward-declared for
// the callable branch in python_to_value).
struct PyPrimOpCallback {
    nb::object func;
    int arity;
    std::string doc;
};

static std::map<std::string, PyPrimOpCallback> &py_primop_registry() {
    static std::map<std::string, PyPrimOpCallback> reg;
    return reg;
}

// Tag type backing the PrimopError Python exception class (see NB_MODULE
// below) -- never actually thrown from C++, it only exists to give
// nb::exception<T> something to register a (unused) translator for. The
// Python class itself, not this tag, is what primops raise and what
// py_primop_bridge below checks for by identity.
struct PrimopErrorTag : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// The registered PrimopError Python type object, set once in NB_MODULE and
// read from py_primop_bridge to recognize it by identity.
static nb::object &primop_error_type() {
    static nb::object type;
    return type;
}

// Set the content of a nix::Value from a Python object, recursively.
// `context`, when non-empty, is attached to every Nix string this
// constructs (see value_to_python_arg's comment for why: it's the
// accumulated context of a primop's whole input, re-attached to its whole
// output since we don't know which output string, if any, "belongs" to
// which input string). Defaults to empty for call sites that aren't
// bridging a primop's return value (e.g. value_from_python converting an
// arbitrary caller-supplied Python object, or a zero-arg callable that
// took no Nix input to begin with) -- there's no context to (correctly)
// invent there.
static void python_to_value(
    nix::EvalState &state, nb::object obj, nix::Value &v,
    const std::string *primop_name = nullptr,
    const nix::NixStringContext &context = {})
{
    if (nb::isinstance<PyValue>(obj)) {
        auto pyv = nb::cast<PyValue>(obj);
        auto *owner = pyv.evalState();
        if (owner != &state) {
            state.error<nix::TypeError>(
                "cannot copy a PyValue from another EvalState").debugThrow();
        } else {
            v = *pyv.checkedValue();
        }
    } else if (obj.is_none()) {
        v.mkNull();
    } else if (nb::isinstance<nb::bool_>(obj)) {
        v.mkBool(nb::cast<bool>(obj));
    } else if (nb::isinstance<nb::int_>(obj)) {
        v.mkInt(nix::NixInt{nb::cast<int64_t>(obj)});
    } else if (nb::isinstance<nb::float_>(obj)) {
        v.mkFloat(nb::cast<double>(obj));
    } else if (nb::isinstance<nb::str>(obj)) {
        auto s = nb::cast<std::string>(obj);
        if (context.empty()) {
            v.mkString(s, state.mem);
        } else {
            v.mkString(s, context, state.mem);
        }
    } else if (nb::isinstance<nb::list>(obj)) {
        auto pyList = nb::cast<nb::list>(obj);
        auto builder = state.buildList(pyList.size());
        for (size_t i = 0; i < pyList.size(); i++) {
            auto *elem = state.allocValue();
            python_to_value(state, pyList[i], *elem, primop_name, context);
            builder[i] = elem;
        }
        v.mkList(builder);
    } else if (nb::isinstance<nb::dict>(obj)) {
        auto pyDict = nb::cast<nb::dict>(obj);
        auto bindings = state.buildBindings(pyDict.size());
        for (auto item : pyDict) {
            auto key = nb::cast<std::string>(nb::str(item.first));
            auto sym = state.symbols.create(key);
            auto *val = state.allocValue();
            python_to_value(state, nb::cast<nb::object>(item.second), *val, primop_name, context);
            bindings.insert(sym, val);
        }
        v.mkAttrs(bindings);
    } else if (nb::isinstance<nb::callable>(obj)) {
        // Python callable → anonymous Nix primop.
        int arity = 0;
        try {
            auto inspect = nb::module_::import_("inspect");
            auto sig = inspect.attr("signature")(obj);
            auto params = nb::cast<nb::object>(sig.attr("parameters"));
            auto builtins = nb::module_::import_("builtins");
            arity = nb::cast<int>(builtins.attr("len")(params));
        } catch (nb::python_error &) {
            arity = 0; // uninspectable → evaluate immediately
        }

        if (arity == 0) {
            nb::object result;
            try {
                result = obj();
            } catch (nb::python_error &e) {
                if (primop_name) {
                    state.error<nix::EvalError>(
                        "%s: Python callable raised: %s",
                        *primop_name, e.what()).debugThrow();
                }
                throw;
            }
            python_to_value(state, result, v, primop_name);
        } else {
            static std::atomic<int> anon_counter{0};
            std::string anon_name = "__anon_primop_" + std::to_string(anon_counter++);

            std::vector<std::string> arg_names;
            for (int i = 0; i < arity; i++)
                arg_names.push_back("x" + std::to_string(i + 1));

            auto &reg = py_primop_registry();
            reg[anon_name] = PyPrimOpCallback{std::move(obj), arity, ""};

            auto impl = [anon_name, arity](
                nix::EvalState &st, const nix::PosIdx pos,
                nix::Value **args, nix::Value &ret) {
                py_primop_bridge(anon_name, arity, st, pos, args, ret);
            };

            auto &ops = anon_primops();
            auto &anon = ops.emplace_back(std::make_unique<nix::PrimOp>(nix::PrimOp{
                .name = anon_name,
                .args = arg_names,
                .arity = static_cast<size_t>(arity),
                .doc = std::nullopt,
                .impl = impl,
            }));

            v.mkPrimOp(anon.get());
        }
    } else {
        if (primop_name) {
            state.error<nix::TypeError>(
                "%s: returned a non JSON-compatible Python value",
                *primop_name).debugThrow();
        }
        state.error<nix::TypeError>(
            "cannot convert non JSON-compatible Python value to Nix").debugThrow();
    }
}

PyValue PyEvalState::value_from_python(nb::object obj) {
    checkThread();
    auto *v = state->allocValue();
    python_to_value(*state, obj, *v);
    return PyValue(v, this, alive);
}

// C++ primop implementation that bridges to Python.
static void py_primop_bridge(
    const std::string &name, int arity,
    nix::EvalState &state, const nix::PosIdx,
    nix::Value **args, nix::Value &ret)
{
    auto &reg = py_primop_registry();
    auto it = reg.find(name);
    if (it == reg.end()) {
        state.error<nix::EvalError>("internal error: primop '%s' not found", name).debugThrow();
    }

    nb::gil_scoped_acquire gil;

    // Accumulates the string context of every argument (see
    // value_to_python_arg's comment) so it can be re-attached to
    // whatever this primop returns.
    nix::NixStringContext context;

    // Build Python args list
    nb::list py_args;
    for (int i = 0; i < arity; i++) {
        {
            nb::gil_scoped_release release;
            state.forceValue(*args[i], nix::noPos);
        }
        py_args.append(value_to_python_arg(state, args[i], name, context));
    }

    // Call Python function
    nb::object result;
    try {
        result = it->second.func(*py_args);
    } catch (nb::python_error &e) {
        // Read the exception's own message directly (str(exc_value)) rather
        // than e.what()'s formatted traceback text -- e.what() renders a
        // full "Traceback (most recent call last): ... \nExcType: message"
        // dump, and the old approach of grabbing everything after the last
        // '\n' assumed that message itself was single-line. A primop raising
        // a genuinely multi-line ValueError (e.g. one violation per line)
        // would have most of its message silently discarded, since the last
        // '\n' then falls inside the message rather than at the
        // traceback/message boundary.
        std::string detail;
        try {
            detail = nb::cast<std::string>(nb::str(e.value()));
        } catch (...) {
            detail = e.what();
        }
        // PrimopError is the dedicated type for a primop deliberately
        // crafting a message for the Nix evaluator to see verbatim.
        // ValueError is kept working the same way for backward
        // compatibility / plain Python code that doesn't import it. Any
        // other exception type is an unexpected internal failure, so keep
        // its type name as a signal that this isn't a deliberate rejection.
        const nb::object &primop_error = primop_error_type();
        bool is_deliberate = e.matches(PyExc_ValueError)
            || (primop_error.is_valid() && e.matches(primop_error));
        if (!is_deliberate) {
            std::string type_name = "Error";
            try {
                type_name = nb::cast<std::string>(nb::str(e.type().attr("__name__")));
            } catch (...) {
            }
            detail = type_name + ": " + detail;
        }
        state.error<nix::EvalError>("%s", detail).debugThrow();
    }

    // Convert result to nix::Value, re-attaching the accumulated input
    // context to every string in the result.
    python_to_value(state, result, ret, &name, context);
}

static void register_primop(
    const std::string &name,
    int arity,
    const std::vector<std::string> &arg_names,
    const std::string &doc,
    nb::object callback)
{
    // Store callback in registry
    auto &reg = py_primop_registry();
    auto [it, _] = reg.insert_or_assign(name, PyPrimOpCallback{callback, arity, doc});
    auto &registered = it->second;

    // Create the C++ PrimOp
    auto impl = [name, arity](nix::EvalState &state, const nix::PosIdx pos,
                               nix::Value **args, nix::Value &ret) {
        py_primop_bridge(name, arity, state, pos, args, ret);
    };

    auto *p = new nix::PrimOp{
        .name = name,
        .args = arg_names,
        .arity = static_cast<size_t>(arity),
        .doc = registered.doc.empty() ? std::optional<std::string>{} : std::optional<std::string>{registered.doc},
        .impl = impl,
    };

    // Register globally
    nix::RegisterPrimOp r(std::move(*p));
    delete p;
}

// =========================================================================
// bindings
// =========================================================================

// Keep the evaluator alive for as long as the value this call returns, and
// keep nothing else alive.
//
// `nb::keep_alive<0, 1>` was here, and it links the result to its receiver
// instead. A chain of selections then holds every link, so one leaf pinned
// every root above it and Nix could collect none of the tree. Measured on 200
// attrsets with one child kept from each: every parent dropped, and Boehm
// freed nothing at all.
//
// The promise that annotation delivers is only ever about the far end of the
// chain. A value must not outlive its `EvalState`: `EvalMemory` owns the AST
// arena, which is a monotonic buffer freed as one block, and `EvalState` owns
// the symbol table. So a surviving thunk holds `Expr *` into freed memory, and
// a surviving attrset holds `Symbol` into a destroyed table -- `attr_names()`
// alone would read it. `PyValue::eval_alive` turns that into an exception
// rather than undefined behaviour, and this keeps it from arising.
//
// Reaching the evaluator directly keeps the promise and drops the links in
// between, which were cost and nothing else.
struct KeepEvaluatorAlive {
    static void precall(PyObject **, size_t, nb::detail::cleanup_list *) {}

    static void postcall(PyObject **args, size_t nargs, nb::handle result) {
        if (nargs == 0 || !result.is_valid())
            return;
        nb::handle receiver = args[0];
        PyValue *value = nb::inst_ptr<PyValue>(receiver);
        nb::object evaluator;
        if (value != nullptr && value->eval != nullptr)
            evaluator = nb::find(*value->eval);
        // The receiver as a fallback, which is what this replaced. It reaches
        // the evaluator the long way, so the promise holds either way. It is
        // only reachable if an `EvalState` has no Python object, which nothing
        // in this project can produce.
        nb::detail::keep_alive(result.ptr(), evaluator.is_valid() ? evaluator.ptr() : receiver.ptr());
    }
};

static void bind_value(nb::module_ &m) {
    nb::class_<PyValue>(m, "Value")
        .def("_release", &PyValue::release,
             "Release this wrapper's RootValue. Internal lifetime-management API.")
        .def("type", &PyValue::type_name)
        .def("type_name", &PyValue::type_name)
        .def("is_null", &PyValue::is_null)
        .def("is_int", &PyValue::is_int)
        .def("is_float", &PyValue::is_float)
        .def("is_bool", &PyValue::is_bool)
        .def("is_string", &PyValue::is_string)
        .def("is_path", &PyValue::is_path)
        .def("is_attrs", &PyValue::is_attrs)
        .def("is_list", &PyValue::is_list)
        .def("is_function", &PyValue::is_function)
        .def("is_thunk", &PyValue::is_thunk)
        .def("as_int", &PyValue::as_int)
        .def("as_float", &PyValue::as_float)
        .def("as_bool", &PyValue::as_bool)
        .def("as_string", &PyValue::as_string)
        .def("force", &PyValue::force)
        .def("force_deep", &PyValue::force_deep)
        .def("realise_string", &PyValue::realise_string)
        .def("realise_argv", &PyValue::realise_argv)
        .def("edit_location", &PyValue::edit_location)
        .def("list_length", &PyValue::list_length)
        .def("list_get", &PyValue::list_get, "idx"_a, nb::call_policy<KeepEvaluatorAlive>())
        .def("attr_names", &PyValue::attr_names)
        .def("has_attr", &PyValue::has_attr, "name"_a)
        .def("attr_get", &PyValue::attr_get, "name"_a, nb::call_policy<KeepEvaluatorAlive>())
        .def("auto_call", &PyValue::auto_call, nb::call_policy<KeepEvaluatorAlive>())
        .def("call", &PyValue::call, "arg"_a, nb::call_policy<KeepEvaluatorAlive>())
        .def("derived_path", &PyValue::derived_path)
        .def("build", [](PyValue &self, nb::object build_store, int build_mode, nb::object eval_store) {
            std::shared_ptr<nix::Store> build_store_ptr = nullptr;
            std::shared_ptr<nix::Store> eval_store_ptr = nullptr;
            if (!build_store.is_none())
                build_store_ptr = nb::cast<std::shared_ptr<nix::Store>>(build_store);
            if (!eval_store.is_none())
                eval_store_ptr = nb::cast<std::shared_ptr<nix::Store>>(eval_store);
            return self.build(build_store_ptr, static_cast<nix::BuildMode>(build_mode), eval_store_ptr);
        }, "build_store"_a = nb::none(), "build_mode"_a = static_cast<int>(nix::bmNormal), "eval_store"_a = nb::none())
        .def("to_python", &PyValue::to_python)
        .def("to_json", &PyValue::to_json, "copy_to_store"_a = false)
        .def("__repr__", &PyValue::repr)
        .def("__str__", &PyValue::as_string);
}

static void bind_eval_state(nb::module_ &m) {
    nb::class_<PyEvalState>(m, "EvalState")
        .def(nb::init<nix::Store &, const std::vector<std::string> &,
                      const PyEvalState::SettingsMap &, const PyEvalState::SettingsMap &>(),
             "store"_a, "search_path"_a = std::vector<std::string>{},
             "eval_settings"_a = PyEvalState::SettingsMap{}, "fetch_settings"_a = PyEvalState::SettingsMap{})
        .def(nb::init<std::shared_ptr<nix::Store>, const std::vector<std::string> &, std::shared_ptr<nix::Store>,
                      const PyEvalState::SettingsMap &, const PyEvalState::SettingsMap &>(),
             "store"_a, "search_path"_a = std::vector<std::string>{}, "build_store"_a = nullptr,
             "eval_settings"_a = PyEvalState::SettingsMap{}, "fetch_settings"_a = PyEvalState::SettingsMap{})
        .def("set_eval_setting", &PyEvalState::set_eval_setting, "name"_a, "value"_a,
             "Apply one registered eval setting to this EvalState's own EvalSettings. "
             "Only affects the live-mutable subset once construction has already happened "
             "(nix-path/pure-eval/restrict-eval etc. must be passed to the constructor).")
        .def("set_fetch_setting", &PyEvalState::set_fetch_setting, "name"_a, "value"_a,
             "Apply one registered fetch setting to this EvalState's own fetchers::Settings.")
        .def("eval_string", &PyEvalState::eval_string,
             "expr"_a, "path"_a = "<string>", nb::keep_alive<0, 1>())
        .def("eval_file", &PyEvalState::eval_file, "path"_a, nb::keep_alive<0, 1>())
        .def("begin_repl", &PyEvalState::begin_repl)
        .def("repl_active", &PyEvalState::repl_active)
        .def("repl_eval_string", &PyEvalState::repl_eval_string,
             "expr"_a, "path"_a = "<string>", nb::keep_alive<0, 1>())
        .def("repl_eval_file", &PyEvalState::repl_eval_file, "path"_a, nb::keep_alive<0, 1>())
        .def("repl_load_file", &PyEvalState::repl_load_file, "path"_a, nb::keep_alive<0, 1>())
        .def("repl_process_line", &PyEvalState::repl_process_line,
             "line"_a, "path"_a = "<string>", nb::keep_alive<0, 1>())
        .def("repl_add_attrs", &PyEvalState::repl_add_attrs, "attrs"_a)
        .def("repl_scope_names", &PyEvalState::repl_scope_names)
        .def("statistics_json", &PyEvalState::statistics_json,
             "Return the evaluation statistics of this evaluator, as a JSON document.\n\n"
             "The report holds the same fields that `NIX_SHOW_STATS=1 nix` prints. The\n"
             "report is unreliable when one process holds more than one evaluator:\n"
             "`nrExprs` and `nrThunks` count the process, because Nix keeps those two\n"
             "counters in a static, and so does the switch that turns counting on. The\n"
             "other thirteen counted fields belong to this evaluator alone. Issue #118\n"
             "tracks the repair.\n\n"
             "`primops`, `functions` and `attributes` tables need the `count-calls` eval\n"
             "setting, which is off by default because the counting costs time.\n\n"
             "Raises RuntimeError on Nix 2.31, which has no such report.")
        .def("reset_file_cache", &PyEvalState::reset_file_cache)
        .def("alloc_value", &PyEvalState::alloc_value, nb::keep_alive<0, 1>())
        .def("value_from_python", &PyEvalState::value_from_python, "obj"_a, nb::keep_alive<0, 1>());
}

// Python owns evaluator threads. Nix enables manual Boehm registration during
// initGC(), but it does not register embedding-created threads itself.
static thread_local bool evaluator_thread_registered = false;

// Defined below, beside the rest of the diagnostic.
static void gc_thread_debug_log(const char *event);

// The one owner of this thread's Boehm registration.
//
// **Boehm scans the stack of a registered thread, and no other stack.**
// `GC_push_all_stacks` (pthread_stop_world.c) walks the thread table, pushes
// the stack of each entry as a root, and aborts with "Collecting from unknown
// thread" only when the *calling* thread is absent. A thread that is absent
// and is not the caller is never visited at all. So an unregistered thread
// that drives an evaluator has two failure modes, and the thread that starts
// a collection picks between them:
//
//  1. The collection starts here. Boehm aborts, and the process dies.
//  2. The collection starts on some other registered thread. There is no
//     abort. This stack is not scanned and this thread is not suspended, so a
//     value that only this stack refers to is unreachable, the collector frees
//     it, and the next read of that pointer gives whatever took the memory.
//
// `PyEvalState::checkThread` already names this hazard, and it answers a
// different question: *which* thread may drive an evaluator. It does not make
// that one thread known to the collector. This does.
//
// One object owns the registration, so that it is undone once. The destructor
// is what covers a thread that registers and then exits: a registration left
// behind names a `pthread_t` that glibc hands to the next thread, and
// `GC_suspend_all` then signals a thread that is gone -- issue #72 when glibc
// answers `EINVAL`, and issue #53 when it faults instead.
namespace {
struct GcThreadRegistration {
    // True when this thread holds the registration and therefore owes the
    // matching release. A `-Dgc=disabled` build still tracks it, because the
    // contract that `enter`/`exit` hold to is not about the collector.
    bool owned = false;

    void acquire() {
        if (owned)
            return;
#if NIX_USE_BOEHMGC
        GC_stack_base stack_base;
        if (GC_get_stack_base(&stack_base) != GC_SUCCESS)
            throw std::runtime_error("could not determine thread stack base for Boehm GC");
        auto result = GC_register_my_thread(&stack_base);
        // `GC_DUPLICATE` is a stale entry for this same `pthread_t`, and this
        // thread takes it over rather than leaving it. Issue #73 gives the
        // whole argument: the entry names this thread, so releasing it later
        // is correct whoever made it.
        if (result != GC_SUCCESS && result != GC_DUPLICATE)
            throw std::runtime_error("could not register this thread with Boehm GC");
#endif
        owned = true;
        gc_thread_debug_log("gc_thread_registration:acquired");
    }

    void release() {
        if (!owned)
            return;
        owned = false;
#if NIX_USE_BOEHMGC
        // **The main thread keeps its registration.** Its stack stays a root
        // until the process ends, so there is no stale entry to leave: the
        // `pthread_t` cannot be handed to another thread while this one runs.
        // Releasing it from a `thread_local` destructor would instead call
        // into the collector during static teardown, after the interpreter
        // finalizes, which is the one moment nothing else here runs.
        if (isMainThread())
            return;
        if (GC_unregister_my_thread() != GC_SUCCESS)
            throw std::runtime_error("could not unregister this thread from Boehm GC");
#endif
        gc_thread_debug_log("gc_thread_registration:released");
    }

    ~GcThreadRegistration() {
        // A throw from a destructor calls `std::terminate`. The release above
        // throws only when Boehm rejects a thread it gave us, and a crash at
        // thread exit would say less than the leak does.
        try {
            release();
        } catch (const std::exception &) {  // NOLINT -- see above
            gc_thread_debug_log("gc_thread_registration:release-failed");
        }
    }
};

thread_local GcThreadRegistration gc_thread_registration;
}  // namespace

// Make Boehm aware of the calling thread, and keep it aware until that thread
// exits. `PyEvalState::init` calls this, so a caller that builds an evaluator
// directly -- with no executor and no `enter_evaluator_thread` -- is safe.
void nanopynix_ensure_gc_thread_registered() { gc_thread_registration.acquire(); }

// DIAGNOSTIC (temporary): logs every GC register/unregister call with its OS
// thread id, gated behind NANOPYNIX_GC_THREAD_DEBUG=1, to correlate against a
// crashing thread's LWP in a post-mortem gdb backtrace and determine whether
// GC_suspend_all's "pthread_kill failed at suspend" abort is hitting a thread
// that was already unregistered (or never registered) by the time it fires.
static bool gc_thread_debug_enabled() {
    static const bool enabled = std::getenv("NANOPYNIX_GC_THREAD_DEBUG") != nullptr;
    return enabled;
}

// Where the log goes. stderr is the default but is *useless for the crash this
// exists to diagnose*: pytest captures at the file-descriptor level by
// default, so these writes land in a per-test buffer that is only printed if
// that test fails -- and a SIGSEGV kills the process with the buffer
// undrained. A full CI run with NANOPYNIX_GC_THREAD_DEBUG=1 that segfaulted
// therefore produced zero diagnostic lines. Setting
// NANOPYNIX_GC_THREAD_DEBUG_FILE to a path writes there instead, outside
// pytest's capture, so the record survives the crash that needs it.
//
// Opened once, O_APPEND, and written with raw write(2) rather than a FILE*:
// unbuffered means nothing is lost when the process dies mid-eval, and
// O_APPEND keeps concurrent writes from separate processes (the forkserver
// worker) from overwriting each other.
static int gc_thread_debug_fd() {
    static const int fd = [] {
        const char *path = std::getenv("NANOPYNIX_GC_THREAD_DEBUG_FILE");
        if (path == nullptr || *path == '\0')
            return STDERR_FILENO;
        int opened = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
        // Falling back to stderr rather than failing: this is a diagnostic,
        // and an unwritable path must not take the evaluator down with it.
        return opened < 0 ? STDERR_FILENO : opened;
    }();
    return fd;
}

static void gc_thread_debug_log(const char *event) {
    if (!gc_thread_debug_enabled())
        return;
    char line[256];
    int length = std::snprintf(line, sizeof(line), "[nanopynix-gc-thread-debug] tid=%ld event=%s\n",
                               osThreadId(), event);
    if (length <= 0)
        return;
    size_t remaining = static_cast<size_t>(length) < sizeof(line) ? static_cast<size_t>(length)
                                                                 : sizeof(line) - 1;
    // Best-effort: a short or failed write loses one diagnostic line, which is
    // strictly better than retrying forever inside GC thread registration.
    ssize_t written = write(gc_thread_debug_fd(), line, remaining);
    (void) written;
}

// libstdc++'s classic std::ctype<char> facet caches narrow()/widen()
// results lazily, unguarded by any lock (it's a plain anonymous-namespace
// global, not a function-local "magic static"). Confirmed via
// ThreadSanitizer in two different call paths that both reach the same
// global 'ctype_c' object: boost::format (via nix::HintFmt, used for
// error/trace messages) and std::regex construction (via
// nix::make_ref<regex>, used by nix's own regex-based helpers). Per
// bits/locale_facets.h:
//   - narrow(char_type, char) writes _M_narrow[(unsigned char)c] directly,
//     per byte value, on every call where that slot is still zero -- there
//     is no guard flag for this overload at all, so *every distinct byte
//     value* has its own independent race window on first use. A one-shot
//     warm-up of a single character (e.g. 'a') only pre-populates that one
//     slot and does nothing for the other 255.
//   - widen(char) is gated by the _M_widen_ok flag (checked-then-set
//     without synchronization, so still technically racy on the very first
//     concurrent call, but at least a single shared flag rather than 256
//     independent unguarded slots).
// Populating every possible byte value for narrow(), and calling widen()
// once, single-threaded and before any evaluator thread can reach either
// path concurrently, permanently avoids both races without touching
// libstdc++ itself.
static void warm_up_ctype_facet() {
    static std::once_flag once;
    std::call_once(once, [] {
        const auto &facet = std::use_facet<std::ctype<char>>(std::locale::classic());
        for (int c = 0; c <= 0xff; ++c)
            facet.narrow(static_cast<char>(c), '\0');
        facet.widen('a');
    });
}

// A build with `-Dgc=disabled` has no collector, so there is no thread to
// register and nothing to collect. **The bookkeeping below stays, and only the
// `GC_*` calls go.** `evaluator_thread_registered` is what makes a second
// `enter` and an unpaired `exit` raise, and those two exceptions are part of
// the contract that `NixThreadExecutor` and its tests hold to. A build that
// dropped them would answer differently for a reason that has nothing to do
// with the collector.
static void enter_evaluator_thread() {
    gc_thread_debug_log("enter_evaluator_thread:begin");
    warm_up_ctype_facet();
    if (evaluator_thread_registered)
        throw std::runtime_error("evaluator thread is already registered with Boehm GC");

    // `GcThreadRegistration` above owns the Boehm side, and it is the same
    // object that `PyEvalState::init` reaches through
    // `nanopynix_ensure_gc_thread_registered`. One owner, so an evaluator
    // built directly on this thread and an executor that later enters it do
    // not each hold a registration that the other undoes.
    gc_thread_registration.acquire();
    evaluator_thread_registered = true;
    gc_thread_debug_log("enter_evaluator_thread:registered");
}

// What the two probes below say when this build has no collector.
//
// A distinct message, and not an error from somewhere else. Issue #47 asks for
// exactly this: a caller that reads a counter which is not there must learn
// that the counter is absent, and not that some unrelated call failed. A test
// asks `build_info()["capabilities"]["boehm_gc"]` first and skips; this is the
// answer for a caller that did not ask.
[[maybe_unused]] static std::runtime_error no_collector_in_this_build(const char *what) {
    return std::runtime_error(
        std::string(what) + ": this build of nanopynix has no Boehm collector. "
        "libexpr was built with `-Dgc=disabled`, so the evaluator allocates and "
        "never releases, and the process is the unit of reclamation.");
}

// `GC_gcollect` from a thread Boehm does not know about is not an error return
// -- it is ABORT("Collecting from unknown thread"), which core-dumps the
// process. Measured, before this guard existed. Refuse first.
static void gc_collect() {
#if !NIX_USE_BOEHMGC
    throw no_collector_in_this_build("cannot collect");
#else
    if (!GC_thread_is_registered())
        throw std::runtime_error(
            "cannot collect from a thread that Boehm GC does not know; "
            "run this on an evaluator thread");
    GC_gcollect();
#endif
}

// The OS thread id of the collector's owner, or 0 before the collector starts.
static std::atomic<long> gc_owner_thread_id{0};

// Start the collector, once, on a thread that never exits.
//
// **Boehm's first thread must outlive every collection.** `GC_thr_init` gives
// bdwgc's one statically allocated `GC_thread` -- `first_thread` -- to
// whichever thread reaches `GC_INIT()` first, and marks that entry
// `DETACHED | MAIN_THREAD`. bdwgc never frees the entry, and no thread exit
// clears its flags. `GC_suspend_all` signals every entry that is neither the
// caller nor `FINISHED`, so an entry that names a thread which has exited is
// `pthread_kill` on a dead thread. That is issues #53, #69 and #72, with three
// outcomes: a fault inside `pthread_kill`, an `EINVAL` return, and the
// 150-retry `resend_lost_signals` abort.
//
// `Session.open` used to reach `nix::initGC` on a `nix-store` executor thread,
// which shuts down with the session: 3 crashes in 4 runs, against 0 in 6 with
// an immortal thread.
//
// **The owner lives here, and not in Python.** A caller that builds an
// `EvalState` directly never passes through `NixCore.initialize`, and the test
// fixtures of this repository do exactly that. A rule that one Python function
// has to obey is a rule that every other entry point breaks.
//
// **`PyEvalState::init` calls this, which is what makes that true.** It used
// to say only what `init_libexpr` did, and `nanopynix.EvalState(store)`
// therefore ran with no collector at all: bdwgc aborts first, in
// `GC_register_my_thread`, and `nix::EvalState`'s own `assertGCInitialized()`
// waits behind it. That is issue #54, and the fork it was found in was
// incidental -- a forked child is simply a process where nothing had called
// `init_libexpr` yet.
//
// Not at import, either. bdwgc installs no atfork handlers unless something
// calls `GC_set_handle_fork` before `GC_INIT`, and `GC_handle_fork` is FALSE
// by default. A forkserver parent that only imports this module must not bring
// the collector up, or every worker child inherits a thread table that nothing
// fixes up.
//
// `std::call_once` is what makes two concurrent sessions safe. The
// `gcInitialised` flag that `nix::initGC` tests is a plain `static bool` with
// no mutex, so two threads can otherwise both enter it.
void nanopynix_start_gc_owner_thread() {
    static std::once_flag once;
    std::call_once(once, [] {
        std::promise<void> ready;
        std::future<void> started = ready.get_future();
        std::thread owner([&ready] {
            setCallingThreadName("nix-gc-owner");
            gc_owner_thread_id.store(osThreadId(), std::memory_order_release);
            nix::initGC();
#if NIX_USE_BOEHMGC
            // TEMPORARY, for issue #70. Remove it with the issue.
            //
            // The differential for "a live pointer sits at a displacement that
            // Boehm does not accept". `GC_push_contents_hdr` consults
            // `GC_valid_offsets`, and it black-lists a reference at an
            // unregistered displacement rather than marking the object. Nix
            // registers 1 to 7, for the 3-bit discriminator that `value.hh`
            // packs into the pointer.
            //
            // With every displacement valid, such a reference marks its object
            // instead. If #70 goes away here, an unregistered displacement is
            // the cause. If #70 survives, that whole class is excluded.
            //
            // **It survived, so that class is excluded.** Measured on the
            // 10-second reproduction that `docs/collector-and-threads.md`
            // gives, at 2.34.8:
            //
            //   arm                                    runs  failures
            //   amplified                                12         3
            //   amplified, every offset valid            12         4
            //
            // The differential kept its value, so it stays until #70 closes:
            // a later change to how Nix packs a `Value` pointer would put this
            // class back on the table, and the arm answers it in two minutes.
            //
            // The direction is always more conservative:
            // `GC_initialize_offsets` marks every offset valid, so the
            // collector retains more and frees less. It runs after
            // `nix::initGC`, and on this thread, so no allocation has happened
            // yet.
            if (const char *all_interior = std::getenv("NANOPYNIX_GC_ALL_INTERIOR_POINTERS");
                all_interior != nullptr && *all_interior == '1')
                GC_set_all_interior_pointers(1);
#endif
            ready.set_value();
            // Park, and never leave. The entry that `GC_thr_init` just made
            // names this thread, and bdwgc removes it at no point. A thread
            // that returns from here takes the process down at the next
            // collection.
            //
            // The promise below is never satisfied, and nothing else holds it,
            // so this wait does not end.
            std::promise<void> forever;
            forever.get_future().wait();
        });
        owner.detach();
        // Return only once the collector is up. Every caller behaves as though
        // `nix::initGC` ran inline, which is what it used to do.
        started.wait();
    });
}

// Whether Boehm knows the calling thread.
//
// This is the probe for the invariant that `nanopynix_bind_expr` sets up: the
// collector comes up at import, so Boehm's `first_thread` is the process's
// main thread, and that thread never exits. A test asks this on the main
// thread before it opens anything.
static bool gc_thread_is_registered() {
#if !NIX_USE_BOEHMGC
    throw no_collector_in_this_build("cannot ask whether Boehm GC knows a thread");
#else
    return GC_thread_is_registered() != 0;
#endif
}

// Register this thread with Boehm behind `GcThreadRegistration`'s back.
//
// **A test tool, and the only way to build the state that issue #73 argues
// about.** `GC_register_my_thread` answers `GC_DUPLICATE` in one case:
// `GC_lookup_thread(pthread_self())` finds an entry without the `FINISHED`
// flag. Every thread that registers is `DETACHED`, so unregistering removes
// its entry, and a live entry can therefore only belong to a thread that
// registered, exited without unregistering, and had its `pthread_t` handed on
// by glibc, which caches thread stacks.
//
// That sequence is not reachable on demand. This makes the same table state
// directly, so a test can hold the behaviour that answers #73: the entry must
// not survive the thread, because `GC_suspend_all` signals every entry that is
// neither the caller nor `FINISHED`.
static void gc_register_this_thread_unowned() {
#if !NIX_USE_BOEHMGC
    throw no_collector_in_this_build("cannot register a thread");
#else
    // **Refuse before bdwgc aborts.** `GC_register_my_thread` calls
    // `ABORT("Threads explicit registering is not previously enabled")` when
    // the collector has not started, because `GC_allow_register_threads` runs
    // inside `nix::initGC`. That abort kills the process, and a test that runs
    // this before any evaluator exists -- a `-k` selection, or a shard that
    // happens to start here -- would die rather than fail.
    if (gc_owner_thread_id.load(std::memory_order_acquire) == 0)
        throw std::runtime_error(
            "the collector has not started, so a thread cannot register with it; "
            "build an EvalState first");
    GC_stack_base stack_base;
    if (GC_get_stack_base(&stack_base) != GC_SUCCESS)
        throw std::runtime_error("could not determine thread stack base for Boehm GC");
    if (GC_register_my_thread(&stack_base) != GC_SUCCESS)
        throw std::runtime_error("could not register this thread with Boehm GC");
#endif
}

// The Boehm counters. `GC_get_heap_size` and its neighbours are unsynchronized
// getters, which is why they are read one after the other rather than through
// GC_get_prof_stats: a caller must already quiesce the evaluator to get a
// meaningful number, and the atomic variant would suggest otherwise.
//
// Signed, although Boehm counts in an unsigned GC_word. `non_gc_bytes` does go
// below zero: Boehm rounds an allocation up to a granule but subtracts the
// block size on the free, so a load of allocate-then-free leaves it a little
// under where it started. Measured at -32 bytes after 500 root values came and
// went. As unsigned that arrives in Python as 2^64 - 32, which reads as a leak
// of 18 exabytes. The drift is the honest number.
static std::map<std::string, int64_t> gc_stats() {
#if !NIX_USE_BOEHMGC
    // An empty map, or a map of zeros, would both read as a measurement. There
    // is nothing to measure.
    throw no_collector_in_this_build("cannot read the collector counters");
#else
    return {
        {"gc_no", static_cast<int64_t>(GC_get_gc_no())},
        {"heap_size", static_cast<int64_t>(GC_get_heap_size())},
        {"free_bytes", static_cast<int64_t>(GC_get_free_bytes())},
        {"memory_use", static_cast<int64_t>(GC_get_memory_use())},
        {"non_gc_bytes", static_cast<int64_t>(GC_get_non_gc_bytes())},
    };
#endif
}

static void exit_evaluator_thread() {
    gc_thread_debug_log("exit_evaluator_thread:begin");
    if (!evaluator_thread_registered)
        throw std::runtime_error("evaluator thread is not registered with Boehm GC");
    gc_thread_registration.release();
    evaluator_thread_registered = false;
    gc_thread_debug_log("exit_evaluator_thread:unregistered");
}

// =========================================================================

void nanopynix_bind_expr(nb::module_ &m) {
    m.doc() = "nanopynix: Nix expr bindings (EvalState, Value)";

    // No flake-primop registration here. There used to be one, duplicating
    // nix_flake.cpp's, because PyEvalState::evalSettingsConfigurators() is a
    // function-local static in a header and each hidden-visibility .so
    // therefore got its own copy -- so nix_flake.cpp's registration did not
    // reach an EvalState constructed through this module. There is one shared
    // object now (nanopynix_modules.hh), so there is one vector, and the
    // registration nix_flake.cpp already does covers every EvalState.

    m.def("init_libexpr", []() {
        nanopynix_start_gc_owner_thread();
        auto &f = nix::experimentalFeatureSettings.experimentalFeatures.get();
        f.insert(nix::Xp::FetchTree);
    }, nb::call_guard<nb::gil_scoped_release>());
    m.def("_enter_evaluator_thread", &enter_evaluator_thread,
          "Internal: register the current dedicated evaluator thread with Boehm GC.");
    m.def("_exit_evaluator_thread", &exit_evaluator_thread,
          "Internal: unregister the current dedicated evaluator thread from Boehm GC.");
    m.def("_gc_collect", &gc_collect, nb::call_guard<nb::gil_scoped_release>(),
          "Internal: run one full Boehm collection now, on an evaluator thread.\n\n"
          "Python's gc.collect() does not reach the Nix heap. A test that drops a "
          "value and wants to see the effect must run both collectors.\n\n"
          "Call it through `EvalSession.run`. Boehm aborts the process on a "
          "collection from a thread it does not know, so this refuses first, with "
          "an exception.\n\n"
          "A build with no collector raises as well. Ask "
          "`build_info()['capabilities']['boehm_gc']` before calling this.");
    m.def("_gc_owner_thread_id", []() { return gc_owner_thread_id.load(std::memory_order_acquire); },
          "Internal: the OS thread id that owns Boehm's `first_thread`, or 0.\n\n"
          "Zero means the collector has not started, which is the state of a "
          "process that imported nanopynix and opened no session.\n\n"
          "That thread must never exit. A test reads `/proc/self/task/<id>` "
          "after a session closes, which is how it proves the thread is still "
          "there. Issues #53, #69 and #72 give the reason.");
    m.def("_gc_thread_is_registered", &gc_thread_is_registered,
          "Internal: whether Boehm GC knows the calling thread.\n\n"
          "The collector starts on a thread of its own, and not on the caller's, "
          "so importing this module registers nothing. A thread answers True "
          "once it builds an `EvalState` or enters the evaluator hook.\n\n"
          "Boehm scans the stack of a registered thread and no other stack, so a "
          "thread that drives an evaluator must answer True. Issue #70 gives the "
          "reason, and the two ways an unregistered one fails.\n\n"
          "A build with no collector raises.");
    m.def("_gc_register_this_thread_unowned", &gc_register_this_thread_unowned,
          "Internal, for tests: register this thread behind the registrar's back.\n\n"
          "This makes the one table state that produces `GC_DUPLICATE` -- a live "
          "entry for this `pthread_t` that nanopynix does not own. Issue #73 "
          "argues that such an entry must be taken over and removed, rather than "
          "left alone, because `GC_suspend_all` signals every entry that is "
          "neither the caller nor `FINISHED`.\n\n"
          "Call it only on a thread that then enters and exits the evaluator "
          "hook. A build with no collector raises.");
    m.def("_gc_stats", &gc_stats,
          "Internal: the Boehm counters, for a test that must measure the Nix heap.\n\n"
          "`non_gc_bytes` is the interesting one. Every `nix::allocRootValue` is one "
          "GC_MALLOC_UNCOLLECTABLE block, so this number moves with the count of live "
          "root values -- including a root that Python bookkeeping cannot see.\n\n"
          "It counts every uncollectable allocation in the process, not only root "
          "values. Compare a delta across one operation, never an absolute.\n\n"
          "A build with no collector raises. Ask "
          "`build_info()['capabilities']['boehm_gc']` before calling this.");
    m.def("parse_nix_path", [](std::optional<std::string> raw) -> std::vector<std::string> {
        std::string value;
        if (raw) {
            value = *raw;
        } else {
            const char *env = std::getenv("NIX_PATH");
            if (env)
                value = env;
        }
        if (value.empty())
            return {};
        auto entries = nix::EvalSettings::parseNixPath(value);
        return {entries.begin(), entries.end()};
    }, "value"_a = nb::none());
    // A lambda over `std::string`, and not the static itself: `isPseudoUrl`
    // takes a `std::string_view`, which would need <nanobind/stl/string_view.h>
    // and a temporary that outlives the call. One copy of a short string is
    // the cheaper answer.
    // `nix::Counter::enabled` is one static for the whole process, and Nix
    // sets it from `NIX_SHOW_STATS` when libnixexpr loads. Every increment of
    // every counter tests it, so Nix made it a static on purpose: a per
    // evaluator flag would be a shared write on the hot path of the evaluator.
    // The scope of this function is therefore the process, and not one
    // evaluator, and `statistics` reports zero for each counted field until
    // something turns it on.
    m.def("set_eval_counters_enabled",
          [](bool enabled) { nix::Counter::enabled = enabled; }, "enabled"_a,
          "Turn the evaluation counters of the process on, or off.\n\n"
          "The counters back the numeric fields of `EvalState.statistics_json`, such\n"
          "as `values`, `envs` and `nrFunctionCalls`. Nix leaves them off unless\n"
          "`NIX_SHOW_STATS` is set, because each increment costs an atomic write.\n\n"
          "This setting belongs to the process, and not to one evaluator, so an\n"
          "evaluator cannot count while another one beside it does not. Issue #118\n"
          "tracks the repair, and the reason that Nix keeps a static.");
    m.def("eval_counters_enabled", []() { return nix::Counter::enabled; },
          "Report whether the evaluation counters of the process are on.");

    m.def("is_pseudo_url",
          [](const std::string &value) { return nix::EvalSettings::isPseudoUrl(value); }, "value"_a,
          "Report whether `lookup_file_arg` downloads this string as a tarball.\n\n"
          "True for a `channel:` reference, and for a URL whose scheme Nix "
          "downloads (`http`, `https`, `file`, `channel`, `git`, `s3`, `ssh`).\n\n"
          "This is the first test that `eval_file` and `EvalState.file` apply to "
          "their argument, so a caller that classifies such an argument itself "
          "asks this rather than repeating the list of schemes. The list belongs "
          "to Nix, and it changes with the Nix version.");
    m.def("list_eval_settings_metadata_json", []() {
        bool readOnlyMode = false;
        nix::EvalSettings evalSettings{readOnlyMode};
        return evalSettings.toJSON().dump();
    });
    m.def("eval_file", &eval_file_impl, "state"_a, "path"_a);
    m.def("register_primop", &register_primop,
          "name"_a, "arity"_a, "arg_names"_a, "doc"_a, "callback"_a,
          "Register a Python function as a Nix builtin. "
          "The callback receives Python-primitive arguments and must return a Python primitive.");

    m.def("_cleanup_primop_registry", [] {
        if (Py_IsInitialized())
            py_primop_registry().clear();
    });

    bind_value(m);
    bind_eval_state(m);
    // Nix's exception classes and their translator live in nix_errors.cpp --
    // all of them, in one translation unit, so that a single translator can
    // own the hierarchy and nanobind's cross-module registration order stops
    // mattering. See that file's header comment.

    // PrimopError: a plain Python exception (never thrown from C++ --
    // PrimopErrorTag only exists to give nb::exception<T> a type to bind
    // to) that a Python primop implementation raises to control exactly
    // what message the Nix evaluator sees, with no type-name prefixing.
    // py_primop_bridge recognizes it by identity via primop_error_type().
    nb::exception<PrimopErrorTag> py_primop_error(m, "PrimopError", PyExc_Exception);
    primop_error_type() = py_primop_error;
}
