#include <memory>
#include <utility>

#include <nanobind/nanobind.h>

#include "nanopynix_modules.hh"
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/typing.h>

#include <nix/fetchers/fetchers.hh>
#include <nix/fetchers/fetch-settings.hh>
#include <nix/fetchers/fetch-to-store.hh>
#include <nix/fetchers/attrs.hh>
#include <nix/store/store-api.hh>

#include <nlohmann/json.hpp>

#include <nanopynix/nix_compat_config.hh>

#include "attrs_util.hh"

namespace nb = nanobind;
using namespace nb::literals;

// =========================================================================
// PyInput — wraps nix::fetchers::Input
// =========================================================================

struct PyInput {
    /// The settings that `input` points at.
    ///
    /// A `fetchers::Input` reads its settings after the parse, so an input
    /// that Python holds must own the settings it was built against. Before
    /// this member the settings were a local of the function below, and the
    /// input pointed at a destroyed object.
    ///
    /// No test on a supported version reaches that pointer through a
    /// `PyInput`, so this half is preventive. `nix_flake.cpp` has the same
    /// defect, and there it fails. See issue #34.
    std::shared_ptr<nix::fetchers::Settings> settings;
    nix::fetchers::Input input;

    PyInput(std::shared_ptr<nix::fetchers::Settings> s, nix::fetchers::Input i)
        : settings(std::move(s)), input(std::move(i)) {}

    std::string to_string() const { return input.to_string(); }
    std::string to_url_string() const { return input.toURLString(); }

    nb::typed<nb::dict, nb::str> to_attrs() const {
        return attrs_to_nb_dict(input.toAttrs());
    }

    std::optional<std::string> get_fingerprint(nix::Store &store) const {
        nb::gil_scoped_release release;
        return input.getFingerprint(store);
    }
};

// =========================================================================
// Free functions
// =========================================================================

static PyInput input_from_url(const std::string &url) {
    // Default fetch settings, on the heap and owned by the result: see the
    // comment on `PyInput::settings`.
    auto settings = std::make_shared<nix::fetchers::Settings>();
    std::optional<nix::fetchers::Input> input;
    {
        nb::gil_scoped_release release;
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
        // Nix 2.36 drops the `fetchers::Settings &` parameter of `fromURL`
        // and of `fromAttrs`. An `Input` no longer holds the settings; each
        // fetch call takes them instead. `PyInput::settings` still owns the
        // settings, because those calls still need them.
        input.emplace(nix::fetchers::Input::fromURL(url));
#else
        input.emplace(nix::fetchers::Input::fromURL(*settings, url));
#endif
    }
    return PyInput(std::move(settings), std::move(*input));
}

static PyInput input_from_attrs(const std::map<std::string, std::string> &attrs) {
    nix::fetchers::Attrs a;
    for (auto &[k, v] : attrs) a[k] = v;
    auto settings = std::make_shared<nix::fetchers::Settings>();
    std::optional<nix::fetchers::Input> input;
    {
        nb::gil_scoped_release release;
#if NANOPYNIX_NIX_VERSION_NUMBER >= NANOPYNIX_NIX_2_36
        input.emplace(nix::fetchers::Input::fromAttrs(std::move(a)));
#else
        input.emplace(nix::fetchers::Input::fromAttrs(*settings, std::move(a)));
#endif
    }
    return PyInput(std::move(settings), std::move(*input));
}

// =========================================================================

static void bind_input(nb::module_ &m) {
    // No constructor. A `PyInput` must own the settings that it points at,
    // and Python cannot build the two halves.
    nb::class_<PyInput>(m, "Input")
        .def("to_string", &PyInput::to_string)
        .def("to_url_string", &PyInput::to_url_string)
        .def("to_attrs", &PyInput::to_attrs)
        .def("get_fingerprint", &PyInput::get_fingerprint, "store"_a)
        .def("__str__", &PyInput::to_string)
        .def("__repr__", [](const PyInput &i) {
            return "Input('" + i.to_string() + "')";
        });
}

// =========================================================================

void nanopynix_bind_fetchers(nb::module_ &m) {
    m.doc() = "nanopynix: Nix fetchers bindings (Input, fetch)";

    m.def("input_from_url", &input_from_url, "url"_a,
          "Create an Input from a URL (e.g. 'github:NixOS/nixpkgs')");
    m.def("input_from_attrs", &input_from_attrs, "attrs"_a,
          "Create an Input from a dict of attributes");
    m.def("list_fetch_settings_metadata_json", []() {
        nix::fetchers::Settings fetchSettings;
        return fetchSettings.toJSON().dump();
    });

    bind_input(m);
}
