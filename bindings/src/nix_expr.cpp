#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/shared_ptr.h>

#include <atomic>
#include <cstdlib>
#include <stdexcept>

#include <nix/expr/eval.hh>
#include <nix/expr/eval-error.hh>
#include <nix/expr/eval-gc.hh>
#include <nix/expr/get-drvs.hh>
#include <nix/expr/value.hh>
#include <nix/expr/attr-set.hh>
#include <nix/expr/primops.hh>
#include <nix/expr/value-to-json.hh>
#include <nix/store/build-result.hh>
#include <nix/store/derived-path.hh>
#include <nix/util/experimental-features.hh>
#include <nix/util/logging.hh>

#include <nlohmann/json.hpp>

#include <nanopynix/nix_compat_config.hh>

#include "py_value.hh"

namespace nb = nanobind;
using namespace nb::literals;

#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_32
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

static nb::dict build_result_to_dict(
        const std::string &drv_path,
        bool success,
        const std::string &status,
        const std::string &error_msg) {
    nb::dict d;
    d["drv_path"] = drv_path;
    d["success"] = success;
    d["status"] = status;
    d["error_msg"] = error_msg;
    return d;
}

static nb::dict build_result_from_kbr(
        const nix::KeyedBuildResult &kbr,
        const nix::StoreDirConfig &store) {
    auto path = kbr.path.to_string(store);
    if (auto *success = kbr.tryGetSuccess())
        return build_result_to_dict(path, true, build_success_status_str(success->status), "");
    if (auto *failure = kbr.tryGetFailure())
        return build_result_to_dict(path, false, build_failure_status_str(failure->status), failure->msg());
    return build_result_to_dict(path, false, "unknown", "");
}
#else
static nb::dict build_result_to_dict(
        const std::string &drv_path,
        bool success,
        const std::string &status,
        const std::string &error_msg) {
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

static nb::dict build_result_from_kbr(
        const nix::KeyedBuildResult &kbr,
        const nix::StoreDirConfig &store) {
    auto result = static_cast<nix::BuildResult>(kbr);
    return build_result_to_dict(kbr.path.to_string(store), result.success(),
                                build_status_str(result.status), result.errorMsg);
}
#endif

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

int64_t PyValue::as_int() const { return static_cast<int64_t>(checkedValue()->integer()); }
double PyValue::as_float() const { return checkedValue()->fpoint(); }
bool PyValue::as_bool() const {
    auto *value = checkedValue();
    if (value->type() == nix::nNull)
        return false;
    return value->boolean();
}

std::string PyValue::as_string() const {
    auto *v = checkedValue();
    if (auto sv = v->c_str()) return std::string(sv);
    if (auto *es = evalState()) {
        nb::gil_scoped_release release;
        return std::string(es->forceStringNoCtx(*v, nix::noPos, ""));
    }
    return "";
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

// to_python and to_json are implemented below.

size_t PyValue::list_length() const {
    auto *v = checkedValue();
    if (v->type() != nix::nList) return 0;
    return v->listSize();
}

PyValue PyValue::list_get(size_t idx) const {
    auto *v = checkedValue();
    if (v->type() != nix::nList) throw std::runtime_error("value is not a list");
    auto size = v->listSize();
    if (idx >= size)
        throw std::out_of_range(
            "list index " + std::to_string(idx) + " out of range for length " + std::to_string(size));
    auto *elem = v->listView()[idx];
    if (auto *es = evalState()) {
        nb::gil_scoped_release release;
        es->forceValue(*elem, nix::noPos);
    }
    return PyValue(elem, eval, eval_alive);
}

std::vector<std::string> PyValue::attr_names() const {
    std::vector<std::string> names;
    auto *v = checkedValue();
    if (v->type() != nix::nAttrs) return names;
    for (auto &attr : *v->attrs())
        names.push_back(std::string(evalState()->symbols[attr.name]));
    return names;
}

bool PyValue::has_attr(const std::string &name) const {
    auto *v = checkedValue();
    if (v->type() != nix::nAttrs) return false;
    auto sym = evalState()->symbols.create(name);
    for (auto &attr : *v->attrs())
        if (attr.name == sym) return true;
    return false;
}

PyValue PyValue::attr_get(const std::string &name) const {
    auto *value = checkedValue();
    if (value->type() != nix::nAttrs) throw std::runtime_error("value is not an attribute set");
    auto *es = evalState();
    auto sym = es->symbols.create(name);
    for (auto &attr : *value->attrs()) {
        if (attr.name == sym) {
            nix::Value *v;
            {
                nb::gil_scoped_release release;
                v = es->allocValue();
                es->forceValue(*attr.value, nix::noPos);
            }
            *v = *attr.value;
            return PyValue(v, eval, eval_alive);
        }
    }
    throw std::runtime_error("attribute '" + name + "' not found");
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
            throw nix::Error("selected value is not a derivation");
        drv_path = package_info->queryDrvPath();
        if (!drv_path)
            throw nix::Error("selected derivation has no drvPath");
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
    for (auto &kbr : results) out.append(build_result_from_kbr(kbr, *store));
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
// PyEvalState out-of-line methods
// =========================================================================

PyValue PyEvalState::eval_string(const std::string &expr, const std::string &path) {
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

PyValue PyEvalState::alloc_value() {
    auto *value = state->allocValue();
    value->mkNull();
    return PyValue(value, this, alive);
}

// ── Handle management ─────────────────────────────────────────

/// Replicate nix_gc_incref / nix_gc_decref from the C API.
/// Handles are allocated by the Python HandleRegistry; the C++ side
/// only manages GC references to keep Value* alive while exported.
#if NIX_USE_BOEHMGC
#  include <boost/unordered/concurrent_flat_map.hpp>
#  include <gc/gc_allocator.h>

using RefCountMap = boost::concurrent_flat_map<
    const void *,
    unsigned int,
    std::hash<const void *>,
    std::equal_to<const void *>,
    traceable_allocator<std::pair<const void * const, unsigned int>>>;

static void _gc_incref(const void *p) {
    static RefCountMap &map = *new RefCountMap();
    map.insert_or_visit({p, 1}, [](auto &kv) { kv.second++; });
}

static void _gc_decref(const void *p) {
    static RefCountMap &map = *new RefCountMap();
    map.erase_if(p, [](auto &kv) { return !--kv.second; });
}
#else
static void _gc_incref(const void *) {}
static void _gc_decref(const void *) {}
#endif

PyValue PyEvalState::export_value(nix::Value *v) {
    _gc_incref(v);
    return PyValue(v, this, alive);
}

void PyEvalState::release_exported_value(PyValue &pyv) {
    _gc_decref(pyv.value);
}

PyValue PyEvalState::eval_file(const std::string &path) {
    nix::Value *v;
    {
        nb::gil_scoped_release release;
        auto sourcePath = state->rootPath(nix::CanonPath(path));
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
    return eval->state.get();
}

nix::Value *PyValue::checkedValue() const {
    (void) evalState();
    if (!value)
        throw std::runtime_error("Nix value has been released");
    return value;
}

// =========================================================================
// PyValue::to_python
// =========================================================================

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
                string = es->forceStringNoCtx(*value, nix::noPos, "");
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
        case nix::nList: {
            auto list = nb::list();
            auto n = value->listSize();
            auto lv = value->listView();
            for (size_t i = 0; i < n; i++)
                list.append(PyValue(lv[i], eval, eval_alive).to_python());
            return list;
        }
        case nix::nAttrs: {
            auto dict = nb::dict();
            if (auto *bindings = value->attrs()) {
                for (auto &attr : *bindings) {
                    nix::Value *v;
                    {
                        nb::gil_scoped_release release;
                        v = es->allocValue();
                        es->forceValue(*attr.value, nix::noPos);
                    }
                    *v = *attr.value;
                    auto key = std::string(es->symbols[attr.name]);
                    dict[nb::str(key.c_str())] = PyValue(v, eval, eval_alive).to_python();
                }
            }
            return dict;
        }
        default:
            return nb::str(type_name().c_str());
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
static nb::object value_to_python_arg(nix::EvalState &state, nix::Value *v, const std::string &primop_name) {
    if (!v) return nb::none();
    switch (v->type()) {
        case nix::nNull:   return nb::none();
        case nix::nInt:    return nb::int_(static_cast<int64_t>(v->integer()));
        case nix::nFloat:  return nb::float_(v->fpoint());
        case nix::nBool:   return nb::bool_(v->boolean());
        case nix::nString: {
            std::string string;
            {
                nb::gil_scoped_release release;
                string = state.forceStringNoCtx(*v, nix::noPos, "");
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
                list.append(value_to_python_arg(state, lv[i], primop_name));
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
                        value_to_python_arg(state, attr.value, primop_name);
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

// Set the content of a nix::Value from a Python object, recursively.
static void python_to_value(
    nix::EvalState &state, nb::object obj, nix::Value &v,
    const std::string *primop_name = nullptr)
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
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_32
        v.mkString(s, state.mem);
#else
        v.mkString(s);
#endif
    } else if (nb::isinstance<nb::list>(obj)) {
        auto pyList = nb::cast<nb::list>(obj);
        auto builder = state.buildList(pyList.size());
        for (size_t i = 0; i < pyList.size(); i++) {
            auto *elem = state.allocValue();
            python_to_value(state, pyList[i], *elem, primop_name);
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
            python_to_value(state, nb::cast<nb::object>(item.second), *val, primop_name);
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
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_32
                .doc = std::nullopt,
#else
                .doc = nullptr,
#endif
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_32
                .impl = impl,
#else
                .fun = impl,
#endif
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

    // Build Python args list
    nb::list py_args;
    for (int i = 0; i < arity; i++) {
        {
            nb::gil_scoped_release release;
            state.forceValue(*args[i], nix::noPos);
        }
        py_args.append(value_to_python_arg(state, args[i], name));
    }

    // Call Python function
    nb::object result;
    try {
        result = it->second.func(*py_args);
    } catch (nb::python_error &e) {
        std::string detail = e.what();
        auto newline = detail.rfind('\n');
        if (newline != std::string::npos) {
            detail = detail.substr(newline + 1);
        }
        auto value_error = std::string("ValueError: ");
        if (detail.rfind(value_error, 0) == 0) {
            detail = detail.substr(value_error.size());
        }
        state.error<nix::EvalError>("%s", detail).debugThrow();
    }

    // Convert result to nix::Value
    python_to_value(state, result, ret, &name);
}

static void register_primop(
    const std::string &name,
    int arity,
    const std::vector<std::string> &arg_names,
    const std::string &doc,
    nb::object callback)
{
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
    throw std::runtime_error(
        "register_primop is unsupported by Nix " NANOPYNIX_NIX_VERSION
        ": this Nix version has a fixed-capacity builtin attribute set");
#else
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
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_32
        .doc = registered.doc.empty() ? std::optional<std::string>{} : std::optional<std::string>{registered.doc},
#else
        .doc = registered.doc.empty() ? nullptr : registered.doc.c_str(),
#endif
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_32
        .impl = impl,
#else
        .fun = impl,
#endif
    };

    // Register globally
    nix::RegisterPrimOp r(std::move(*p));
    delete p;
#endif
}

// =========================================================================
// bindings
// =========================================================================

static void bind_value(nb::module_ &m) {
    nb::class_<PyValue>(m, "Value")
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
        .def("list_length", &PyValue::list_length)
        .def("list_get", &PyValue::list_get, "idx"_a, nb::keep_alive<0, 1>())
        .def("attr_names", &PyValue::attr_names)
        .def("has_attr", &PyValue::has_attr, "name"_a)
        .def("attr_get", &PyValue::attr_get, "name"_a, nb::keep_alive<0, 1>())
        .def("auto_call", &PyValue::auto_call, nb::keep_alive<0, 1>())
        .def("call", &PyValue::call, "arg"_a, nb::keep_alive<0, 1>())
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
        .def(nb::init<nix::Store &, const std::vector<std::string> &>(),
             "store"_a, "search_path"_a = std::vector<std::string>{})
        .def(nb::init<std::shared_ptr<nix::Store>, const std::vector<std::string> &>(),
             "store"_a, "search_path"_a = std::vector<std::string>{})
        .def("eval_string", &PyEvalState::eval_string,
             "expr"_a, "path"_a = "<string>", nb::keep_alive<0, 1>())
        .def("eval_file", &PyEvalState::eval_file, "path"_a, nb::keep_alive<0, 1>())
        .def("alloc_value", &PyEvalState::alloc_value, nb::keep_alive<0, 1>())
        // Handle management (worker-internal, exported for RPC dispatch)
        // The Python HandleRegistry owns handle allocation; C++ manages GC refs.
        .def("export_value", &PyEvalState::export_value, "value"_a,
             nb::keep_alive<0, 1>(),
             "Take a GC reference on *value and return a PyValue wrapper.")
        .def("release_exported_value", &PyEvalState::release_exported_value, "pyv"_a,
             "Release the GC reference held by an exported PyValue.")
        .def("value_from_python", &PyEvalState::value_from_python, "obj"_a, nb::keep_alive<0, 1>())
        .def("_export_pyvalue", [](PyEvalState &es, PyValue &pyv) {
            return es.export_value(pyv.checkedValue());
        }, nb::keep_alive<0, 1>(), "pyv"_a);
}

// =========================================================================

NB_MODULE(nanopynix_expr, m) {
    m.doc() = "nanopynix: Nix expr bindings (EvalState, Value)";

    m.def("init_libexpr", []() {
        nix::initGC();
        auto &f = nix::experimentalFeatureSettings.experimentalFeatures.get();
        f.insert(nix::Xp::FetchTree);
    }, nb::call_guard<nb::gil_scoped_release>());
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

    m.def("_set_pure_eval", [](bool pure) {
        PyEvalState::evalSettingsConfigurators().push_back(
            [=](nix::EvalSettings &es) { es.pureEval = pure; }
        );
    });

    m.def("_set_restrict_eval", [](bool restrict_) {
        PyEvalState::evalSettingsConfigurators().push_back(
            [=](nix::EvalSettings &es) { es.restrictEval = restrict_; }
        );
    });

    m.def("_set_allowed_uris", [](const std::vector<std::string> &uris) {
        static std::vector<std::string> desired;
        desired = uris;
        PyEvalState::evalSettingsConfigurators().push_back(
            [](nix::EvalSettings &es) {
                auto &s = es.allowedUris;
                s.set("", false);  // clear
                for (auto &u : desired)
                    s.set(u, true);
            }
        );
    });

    m.def("_cleanup_primop_registry", [] {
        if (Py_IsInitialized())
            py_primop_registry().clear();
    });

    bind_value(m);
    bind_eval_state(m);

    // ── Exception bindings (LIFO: last registered tried first.
    // Register base classes FIRST so specific subclasses are tried before them)
    nb::exception<nix::EvalError> py_eval_err(m, "EvalError", PyExc_RuntimeError);
    nb::exception<nix::ParseError> py_parse_err(m, "ParseError", PyExc_RuntimeError);
    nb::exception<nix::TypeError> py_type_err(m, "TypeError", PyExc_RuntimeError);
    nb::exception<nix::UndefinedVarError> py_undef_err(m, "UndefinedVarError", PyExc_RuntimeError);
    nb::exception<nix::AssertionError> py_assert_err(m, "AssertionError", PyExc_RuntimeError);
    nb::exception<nix::ThrownError> py_thrown_err(m, "ThrownError", PyExc_RuntimeError);
    (void) py_eval_err;
    (void) py_parse_err;
    (void) py_type_err;
    (void) py_undef_err;
    (void) py_assert_err;
    (void) py_thrown_err;
}
