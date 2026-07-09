#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/shared_ptr.h>

#include <nix/expr/eval.hh>
#include <nix/expr/eval-error.hh>
#include <nix/expr/eval-gc.hh>
#include <nix/expr/value.hh>
#include <nix/expr/attr-set.hh>
#include <nix/expr/primops.hh>

#include "py_eval.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// PyValue
// =========================================================================

struct PyValue {
    nix::Value *value;
    std::shared_ptr<PyEvalState> eval;

    PyValue(nix::Value *v, std::shared_ptr<PyEvalState> e) : value(v), eval(std::move(e)) {}

    std::string type_name() {
        if (!value) return "null";
        switch (value->type()) {
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

    bool is_null()     const { return value && value->type() == nix::nNull; }
    bool is_int()      const { return value && value->type() == nix::nInt; }
    bool is_float()    const { return value && value->type() == nix::nFloat; }
    bool is_bool()     const { return value && value->type() == nix::nBool; }
    bool is_string()   const { return value && value->type() == nix::nString; }
    bool is_path()     const { return value && value->type() == nix::nPath; }
    bool is_attrs()    const { return value && value->type() == nix::nAttrs; }
    bool is_list()     const { return value && value->type() == nix::nList; }
    bool is_function() const { return value && value->type() == nix::nFunction; }
    bool is_thunk()    const { return value && value->type() == nix::nThunk; }

    int64_t as_int() const { return static_cast<int64_t>(value->integer()); }
    double as_float() const { return value->fpoint(); }
    bool as_bool() const { return value->boolean(); }

    std::string as_string() const {
        if (auto sv = value->c_str()) return std::string(sv);
        if (auto *es = evalState())
            return std::string(es->forceStringNoCtx(*value, nix::noPos, ""));
        return "";
    }

    void force() { if (auto *es = evalState()) es->forceValue(*value, nix::noPos); }
    void force_deep() { if (auto *es = evalState()) es->forceValueDeep(*value); }

    nb::object to_python();

    size_t list_length() const {
        if (value->type() != nix::nList) return 0;
        return value->listSize();
    }
    PyValue list_get(size_t idx) const {
        if (value->type() != nix::nList) throw std::runtime_error("value is not a list");
        auto *elem = value->listView()[idx];
        if (auto *es = evalState()) es->forceValue(*elem, nix::noPos);
        return PyValue(elem, eval);
    }

    std::vector<std::string> attr_names() const {
        std::vector<std::string> names;
        if (value->type() != nix::nAttrs) return names;
        for (auto &attr : *value->attrs())
            names.push_back(std::string(evalState()->symbols[attr.name]));
        return names;
    }

    bool has_attr(const std::string &name) const {
        if (value->type() != nix::nAttrs) return false;
        auto sym = evalState()->symbols.create(name);
        for (auto &attr : *value->attrs())
            if (attr.name == sym) return true;
        return false;
    }

    PyValue attr_get(const std::string &name) const {
        if (value->type() != nix::nAttrs) throw std::runtime_error("value is not an attribute set");
        auto *es = evalState();
        auto sym = es->symbols.create(name);
        for (auto &attr : *value->attrs()) {
            if (attr.name == sym) {
                auto *v = es->allocValue();
                es->forceValue(*attr.value, nix::noPos);
                *v = *attr.value;
                return PyValue(v, eval);
            }
        }
        throw std::runtime_error("attribute '" + name + "' not found");
    }

    PyValue call(PyValue arg) {
        auto *es = evalState();
        auto *result = es->allocValue();
        es->callFunction(*value, *arg.value, *result, nix::noPos);
        es->forceValue(*result, nix::noPos);
        return PyValue(result, eval);
    }

    std::string repr() {
        if (!value) return "PyValue(null)";
        return "PyValue(" + type_name() + ")";
    }

private:
    nix::EvalState *evalState() const;
};

// =========================================================================
// PyEvalState out-of-line methods
// =========================================================================

PyValue PyEvalState::eval_string(const std::string &expr, const std::string &path) {
    auto *parsedExpr = state->parseExprFromString(
        expr, state->rootPath(nix::CanonPath(path)));
    auto *v = state->allocValue();
    state->eval(parsedExpr, *v);
    state->forceValue(*v, nix::noPos);
    return PyValue(v, evalRef());
}

PyValue PyEvalState::alloc_value() {
    return PyValue(state->allocValue(), evalRef());
}

// ── Handle management ─────────────────────────────────────────

/// Replicate nix_gc_incref / nix_gc_decref from the C API.
/// Uses a traceable-allocated map so Boehm GC sees the Value*
/// pointers and keeps referenced Values alive.
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

static std::map<int64_t, nix::Value *> &_handle_registry() {
    thread_local std::map<int64_t, nix::Value *> reg;
    return reg;
}
#else
static void _gc_incref(const void *) {}
static void _gc_decref(const void *) {}
static std::map<int64_t, nix::Value *> &_handle_registry() {
    thread_local std::map<int64_t, nix::Value *> reg;
    return reg;
}
#endif

int64_t PyEvalState::export_value(nix::Value *v) {
    int64_t h = _next_handle++;
    _gc_incref(v);
    _handle_registry()[h] = v;
    return h;
}

nix::Value *PyEvalState::get_exported(int64_t handle) {
    auto &reg = _handle_registry();
    auto it = reg.find(handle);
    if (it == reg.end())
        throw std::runtime_error("handle " + std::to_string(handle) + " not found");
    return it->second;
}

PyValue PyEvalState::value_from_handle(int64_t handle) {
    return PyValue(get_exported(handle), evalRef());
}

void PyEvalState::release_exported(int64_t handle) {
    auto &reg = _handle_registry();
    auto it = reg.find(handle);
    if (it != reg.end()) {
        _gc_decref(it->second);
        reg.erase(it);
    }
}

void PyEvalState::release_all_exported() {
    for (auto &[h, v] : _handle_registry())
        _gc_decref(v);
    _handle_registry().clear();
}

PyValue PyEvalState::eval_file(const std::string &path) {
    auto sourcePath = state->rootPath(nix::CanonPath(path));
    auto *v = state->allocValue();
    state->evalFile(sourcePath, *v);
    // Do NOT force — the caller may want to navigate lazily.
    return PyValue(v, evalRef());
}

// =========================================================================
// PyValue::evalState
// =========================================================================

nix::EvalState *PyValue::evalState() const {
    return eval ? eval->state.get() : nullptr;
}

// =========================================================================
// PyValue::to_python
// =========================================================================

nb::object PyValue::to_python() {
    auto *es = evalState();
    if (!es || !value) return nb::none();

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
        case nix::nString:
            return nb::str(std::string(es->forceStringNoCtx(*value, nix::noPos, "")).c_str());
        case nix::nPath: {
            nix::NixStringContext ctx;
            return nb::str(std::string(es->coerceToPath(
                nix::noPos, *value, ctx, "").path.abs()).c_str());
        }
        case nix::nList: {
            auto list = nb::list();
            auto n = value->listSize();
            auto lv = value->listView();
            for (size_t i = 0; i < n; i++)
                list.append(PyValue(lv[i], eval).to_python());
            return list;
        }
        case nix::nAttrs: {
            auto dict = nb::dict();
            if (auto *bindings = value->attrs()) {
                for (auto &attr : *bindings) {
                    auto *v = es->allocValue();
                    es->forceValue(*attr.value, nix::noPos);
                    *v = *attr.value;
                    auto key = std::string(es->symbols[attr.name]);
                    dict[nb::str(key.c_str())] = PyValue(v, eval).to_python();
                }
            }
            return dict;
        }
        default:
            return nb::str(type_name().c_str());
    }
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
        case nix::nString: return nb::str(std::string(state.forceStringNoCtx(*v, nix::noPos, "")).c_str());
        case nix::nPath: {
            nix::NixStringContext ctx;
            return nb::str(std::string(state.coerceToPath(nix::noPos, *v, ctx, "").path.abs()).c_str());
        }
        case nix::nList: {
            auto list = nb::list();
            auto n = v->listSize();
            auto lv = v->listView();
            for (size_t i = 0; i < n; i++) {
                state.forceValue(*lv[i], nix::noPos);
                list.append(value_to_python_arg(state, lv[i], primop_name));
            }
            return list;
        }
        case nix::nAttrs: {
            auto dict = nb::dict();
            if (auto *bindings = v->attrs()) {
                for (auto &attr : *bindings) {
                    state.forceValue(*attr.value, nix::noPos);
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

// Set the content of a nix::Value from a Python object, recursively.
static void python_to_value(
    nix::EvalState &state, nb::object obj, nix::Value &v,
    const std::string *primop_name = nullptr)
{
    if (nb::isinstance<PyValue>(obj)) {
        auto pyv = nb::cast<PyValue>(obj);
        if (!pyv.value) {
            v.mkNull();
        } else if (pyv.eval && pyv.eval->state.get() != &state) {
            state.error<nix::TypeError>(
                "cannot copy a PyValue from another EvalState").debugThrow();
        } else {
            v = *pyv.value;
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
        v.mkString(s, state.mem);
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
    return PyValue(v, evalRef());
}

// Holder for a registered Python primop callback.
struct PyPrimOpCallback {
    nb::object func;
    int arity;
};

// Global registry: primop name → callback
static std::map<std::string, PyPrimOpCallback> &py_primop_registry() {
    static std::map<std::string, PyPrimOpCallback> reg;
    return reg;
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
        state.forceValue(*args[i], nix::noPos);
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
    // Store callback in registry
    auto &reg = py_primop_registry();
    reg[name] = PyPrimOpCallback{callback, arity};

    // Create the C++ PrimOp
    auto impl = [name, arity](nix::EvalState &state, const nix::PosIdx pos,
                               nix::Value **args, nix::Value &ret) {
        py_primop_bridge(name, arity, state, pos, args, ret);
    };

    auto *p = new nix::PrimOp{
        .name = name,
        .args = arg_names,
        .arity = static_cast<size_t>(arity),
        .doc = doc.empty() ? std::optional<std::string>{} : std::optional<std::string>{doc},
        .impl = impl,
    };

    // Register globally
    nix::RegisterPrimOp r(std::move(*p));
    delete p;
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
        .def("list_get", &PyValue::list_get, "idx"_a)
        .def("attr_names", &PyValue::attr_names)
        .def("has_attr", &PyValue::has_attr, "name"_a)
        .def("attr_get", &PyValue::attr_get, "name"_a)
        .def("call", &PyValue::call, "arg"_a)
        .def("to_python", &PyValue::to_python)
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
             "expr"_a, "path"_a = "<string>")
        .def("eval_file", &PyEvalState::eval_file, "path"_a)
        .def("alloc_value", &PyEvalState::alloc_value)
        // Handle management (worker-internal, exported for RPC dispatch)
        .def("export_value", &PyEvalState::export_value, "value"_a,
             "Export a Value and return a GC-safe integer handle.")
        .def("get_exported", &PyEvalState::get_exported, "handle"_a,
             "Get a Value by its export handle.")
        .def("release_exported", &PyEvalState::release_exported, "handle"_a,
             "Release an exported handle.")
        .def("release_all_exported", &PyEvalState::release_all_exported,
             "Release all exported handles.")
        .def("value_from_handle", &PyEvalState::value_from_handle, "handle"_a)
        .def("value_from_python", &PyEvalState::value_from_python, "obj"_a)
        .def("_export_pyvalue", [](PyEvalState &es, PyValue &pyv) {
            return es.export_value(pyv.value);
        }, "pyv"_a);
}

// =========================================================================

NB_MODULE(nanopynix_expr, m) {
    m.doc() = "nanopynix: Nix expr bindings (EvalState, Value)";

    m.def("init_libexpr", []() { nix::initGC(); });
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
