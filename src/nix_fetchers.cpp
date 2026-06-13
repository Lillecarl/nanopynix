#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/map.h>
#include <nanobind/typing.h>

#include <variant>
#include <type_traits>

#include <nix/fetchers/fetchers.hh>
#include <nix/fetchers/fetch-settings.hh>
#include <nix/fetchers/fetch-to-store.hh>
#include <nix/fetchers/attrs.hh>
#include <nix/store/store-api.hh>

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
        auto attrs = input.toAttrs();
        nb::dict d;
        for (auto &[k, v] : attrs) {
            std::visit([&](auto &&val) {
                using T = std::decay_t<decltype(val)>;
                if constexpr (std::is_same_v<T, std::string>)
                    d[nb::str(k.c_str())] = nb::str(val.c_str());
                else if constexpr (std::is_same_v<T, uint64_t>)
                    d[nb::str(k.c_str())] = nb::int_(val);
                else if constexpr (std::is_same_v<T, nix::Explicit<bool>>)
                    d[nb::str(k.c_str())] = nb::bool_(val.t);
            }, v);
        }
        return d;
    }

    std::optional<std::string> get_fingerprint(nix::Store &store) const {
        return input.getFingerprint(store);
    }
};

// =========================================================================
// Free functions
// =========================================================================

static PyInput input_from_url(const std::string &url) {
    // Use default fetch settings (global nix::settings provides this)
    // but Input::fromURL needs fetchers::Settings — we create a local one
    nix::fetchers::Settings fetchSettings;
    return PyInput(nix::fetchers::Input::fromURL(fetchSettings, url));
}

static PyInput input_from_attrs(const std::map<std::string, std::string> &attrs) {
    nix::fetchers::Attrs a;
    for (auto &[k, v] : attrs) a[k] = v;
    nix::fetchers::Settings fetchSettings;
    return PyInput(nix::fetchers::Input::fromAttrs(fetchSettings, std::move(a)));
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

NB_MODULE(nanopynix_fetchers, m) {
    m.doc() = "nanopynix: Nix fetchers bindings (Input, fetch)";

    m.def("input_from_url", &input_from_url, "url"_a,
          "Create an Input from a URL (e.g. 'github:NixOS/nixpkgs')");
    m.def("input_from_attrs", &input_from_attrs, "attrs"_a,
          "Create an Input from a dict of attributes");

    bind_input(m);
}
