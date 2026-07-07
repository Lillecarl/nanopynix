#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <nanobind/nanobind.h>

#include <nix/expr/eval.hh>
#include <nix/expr/eval-settings.hh>
#include <nix/expr/value.hh>
#include <nix/fetchers/fetch-settings.hh>
#include <nix/store/store-api.hh>
#include <nix/util/ref.hh>

struct PyValue;

struct PyEvalState {
    std::shared_ptr<nix::Store> store;
    bool _readOnlyMode = false;
    nix::fetchers::Settings fetchSettings;
    nix::EvalSettings evalSettings{_readOnlyMode};
    std::shared_ptr<nix::EvalState> state;

    PyEvalState(std::shared_ptr<nix::Store> s,
                const std::vector<std::string> &searchPath = {})
        : store(std::move(s))
    {
        init(searchPath);
    }

    PyEvalState(nix::Store &s, const std::vector<std::string> &searchPath = {})
        : store(s.shared_from_this())
    {
        init(searchPath);
    }

    PyValue eval_string(const std::string &expr, const std::string &path = "<string>");
    PyValue eval_file(const std::string &path);
    PyValue alloc_value();

    // ── Handle management ───────────────────────────────────────

    /// Export a Value and return an opaque integer handle.
    int64_t export_value(nix::Value *v);

    /// Look up a previously exported Value by handle.
    nix::Value *get_exported(int64_t handle);

    /// Look up and wrap in a PyValue (Python-safe).
    PyValue value_from_handle(int64_t handle);

    /// Convert a JSON-compatible Python object into a Nix Value.
    PyValue value_from_python(nanobind::object obj);

    /// Release a handle.
    void release_exported(int64_t handle);

    /// Release all exported handles.
    void release_all_exported();

    std::shared_ptr<PyEvalState> evalRef() {
        return std::shared_ptr<PyEvalState>(this, [](PyEvalState *){});
    }

private:
    int64_t _next_handle = 1;

    void init(const std::vector<std::string> &searchPath) {
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
            evalSettings);
    }
};
