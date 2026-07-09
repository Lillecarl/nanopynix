#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <nanobind/nanobind.h>

#include <nix/expr/eval.hh>
#include <nix/expr/value.hh>

#include "py_eval.hh"

struct PyValue {
    nix::Value *value;
    std::shared_ptr<PyEvalState> eval;

    PyValue(nix::Value *v, std::shared_ptr<PyEvalState> e) : value(v), eval(std::move(e)) {}

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

    void force();
    void force_deep();

    nanobind::object to_python();
    nanobind::object to_json(bool copy_to_store = false);

    size_t list_length() const;
    PyValue list_get(size_t idx) const;

    std::vector<std::string> attr_names() const;
    bool has_attr(const std::string &name) const;
    PyValue attr_get(const std::string &name) const;

    PyValue call(PyValue arg);

    std::string repr();

private:
    nix::EvalState *evalState() const;
};
