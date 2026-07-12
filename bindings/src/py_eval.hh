#pragma once

#include <cstdint>
#include <functional>
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
    std::shared_ptr<bool> alive = std::make_shared<bool>(true);

    using EvalSettingsConfigurator = std::function<void(nix::EvalSettings &)>;

    static std::vector<EvalSettingsConfigurator> &evalSettingsConfigurators() {
        static std::vector<EvalSettingsConfigurator> configurators;
        return configurators;
    }

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

    ~PyEvalState() {
        if (alive)
            *alive = false;
    }

    PyValue eval_string(const std::string &expr, const std::string &path = "<string>");
    PyValue eval_file(const std::string &path);
    PyValue alloc_value();

    // ── Handle management ───────────────────────────────────────

    /// Take a GC reference on *v and return a PyValue wrapper.
    /// The caller must eventually call release_exported_value() on the result.
    PyValue export_value(nix::Value *v);

    /// Release the GC reference held by an exported PyValue.
    void release_exported_value(PyValue &pyv);

    /// Convert a JSON-compatible Python object into a Nix Value.
    PyValue value_from_python(nanobind::object obj);

private:
    void init(const std::vector<std::string> &searchPath) {
        for (auto &cfg : evalSettingsConfigurators())
            cfg(evalSettings);

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
