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

// Refuse a `nix::Value *` that cannot be one, and say so where it enters.
//
// **A misaligned value pointer is the fingerprint of issue #70.**
// `nix::ValueStorage` packs a 3-bit discriminator into the low bits of its
// first word, so it is `alignas(16)` and every real `Value *` has those bits
// clear. A pointer that keeps them is the second word of some *other* value,
// read through a block that the collector gave away and that now holds
// something else.
//
// The core dump of that crash is in `docs/collector-and-threads.md`.
//
// **It does not catch #70, and that is a measurement.** Fourteen runs of the
// short reproduction, two of which died with SIGSEGV: the check fired in
// neither. So the bad pointer never crosses this boundary. `ExprVar::eval`
// reads it out of an `Env` and faults on it inside libnixexpr, and no
// `PyValue` ever holds it. That narrows the search, and it is the reason this
// comment does not promise what the check cannot do.
//
// It stays because the invariant is real and the cost is one `and` and one
// predicted branch: a value pointer that reaches an accessor with those bits
// set is a defect whatever produced it, and a later path of ours -- the rpc
// worker rebuilding a value from a handle, say -- can produce one.
//
// The mask comes from `alignof`, and not from the constant 16, so a build
// whose `Value` is not bit-packed checks whatever that build really needs.
//
// Always on. A check that runs only in a debug build cannot report a defect
// that appears once in four runs of CI.
inline void nanopynix_check_value_alignment(const nix::Value *v, const char *where) {
    constexpr std::uintptr_t mask = alignof(nix::Value) - 1;
    auto bits = reinterpret_cast<std::uintptr_t>(v);
    if (v != nullptr && (bits & mask) != 0)
        throw std::runtime_error(
            std::string("nanopynix: ") + where + " received a Nix value pointer that is not "
            "aligned, which means it is not a value. Issue #70 gives the analysis. "
            "A `nix::Value` needs an address that is a multiple of " +
            std::to_string(alignof(nix::Value)) + ", and this one is " +
            std::to_string(bits & mask) + " past such an address.");
}

struct PyValue {
    nix::RootValue root;
    PyEvalState *eval;
    std::shared_ptr<bool> eval_alive;

    PyValue(nix::Value *v, PyEvalState *e, std::shared_ptr<bool> alive)
        : root((nanopynix_check_value_alignment(v, "a new value wrapper"), nix::allocRootValue(v))),
          eval(e),
          eval_alive(std::move(alive)) {}

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
