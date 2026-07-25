#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <nanobind/nanobind.h>

#include <nix/expr/eval.hh>
#include <nix/expr/value.hh>
#include <nix/store/store-api.hh>

#include "py_eval.hh"

struct PyValue {
    nix::RootValue root;
    PyEvalState *eval;
    std::shared_ptr<bool> eval_alive;

    PyValue(nix::Value *v, PyEvalState *e, std::shared_ptr<bool> alive)
        : root(nix::allocRootValue(v)), eval(e), eval_alive(std::move(alive)) {}

    void release() { root.reset(); }

    std::string type_name();
    bool is_null() const;
    bool is_int() const;
    bool is_float() const;
    bool is_bool() const;
    bool is_string() const;
    bool is_path() const;
    bool is_attrs() const;
    bool is_list() const;
    bool is_function() const;
    bool is_thunk() const;

    int64_t as_int() const;
    double as_float() const;
    bool as_bool() const;
    std::string as_string() const;
    std::string coerce_to_string() const;

    void force();
    void force_deep();
    std::string realise_string();
    std::vector<std::string> realise_argv();
    nanobind::dict edit_location();

    nanobind::object to_python();
    nanobind::object to_json(bool copy_to_store = false);

    size_t list_length() const;
    PyValue list_get(size_t idx) const;

    std::vector<std::string> attr_names() const;
    bool has_attr(const std::string &name) const;
    PyValue attr_get(const std::string &name) const;

    PyValue auto_call();
    PyValue call(PyValue arg);
    std::string derived_path();
    nanobind::dict build(
        std::shared_ptr<nix::Store> build_store = nullptr,
        nix::BuildMode build_mode = nix::bmNormal,
        std::shared_ptr<nix::Store> eval_store = nullptr);

    std::string repr();

    nix::EvalState *evalState() const;
    /// `evalState()` but never null -- the as_* accessors cannot work without it.
    nix::EvalState &requireEvalState() const;
    nix::Value *checkedValue() const;
};
