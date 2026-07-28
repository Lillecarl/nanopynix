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
    nix::fetchers::Input input;

    PyInput(nix::fetchers::Input i) : input(std::move(i)) {}

    std::string to_string() const { return input.to_string(); }
    std::string to_url_string() const { return input.toURLString(); }

    nb::typed<nb::dict, nb::str> to_attrs() const {
        return attrs_to_nb_dict(input.toAttrs());
    }

    std::optional<std::string> get_fingerprint(nix::Store &store) const {
        nb::gil_scoped_release release;
#if NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_32
        return input.getFingerprint(nix::ref<nix::Store>(store.shared_from_this()));
#elif NANOPYNIX_NIX_VERSION_NUMBER < NANOPYNIX_NIX_2_35
        return input.getFingerprint(store);
#else
        return input.getFingerprint(store);
#endif
    }
};

// =========================================================================
// Free functions
// =========================================================================

static PyInput input_from_url(const std::string &url) {
    // Use default fetch settings (global nix::settings provides this)
    // but Input::fromURL needs fetchers::Settings — we create a local one
    nix::fetchers::Settings fetchSettings;
    std::optional<nix::fetchers::Input> input;
    {
        nb::gil_scoped_release release;
        input.emplace(nix::fetchers::Input::fromURL(fetchSettings, url));
    }
    return PyInput(std::move(*input));
}

static PyInput input_from_attrs(const std::map<std::string, std::string> &attrs) {
    nix::fetchers::Attrs a;
    for (auto &[k, v] : attrs) a[k] = v;
    nix::fetchers::Settings fetchSettings;
    std::optional<nix::fetchers::Input> input;
    {
        nb::gil_scoped_release release;
        input.emplace(nix::fetchers::Input::fromAttrs(fetchSettings, std::move(a)));
    }
    return PyInput(std::move(*input));
}

// =========================================================================

static void bind_input(nb::module_ &m) {
    nb::class_<PyInput>(m, "Input")
        .def(nb::init<nix::fetchers::Input>(), "input"_a)
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
